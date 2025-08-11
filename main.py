# main.py
import os, sys, json, datetime as dt
from pathlib import Path
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import yaml, feedparser, requests
from bs4 import BeautifulSoup
from readability import Document
import trafilatura
from rapidfuzz import fuzz, process

# -------------------- ENV / CONFIG --------------------
ELEVEN_API_KEY  = os.getenv("ELEVEN_API_KEY", "").strip()
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "").strip()
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_ITEMS       = int(os.getenv("MAX_ITEMS", "10"))

ROOT       = Path(".")
PUBLIC_DIR = ROOT / "public"
EP_DIR     = PUBLIC_DIR / "episodes"
SH_NOTES   = PUBLIC_DIR / "shownotes"
for d in (PUBLIC_DIR, EP_DIR, SH_NOTES):
    d.mkdir(parents=True, exist_ok=True)

# -------------------- LOAD FEEDS --------------------
feeds_path = ROOT / "feeds.yml"
if feeds_path.exists():
    with open(feeds_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
else:
    cfg = {"sources": []}
SOURCES = cfg.get("sources", [])
EXCLUDE = set(str(k).lower() for k in cfg.get("exclude_keywords", []))
LIMIT_PER = int(cfg.get("daily_limit_per_source", cfg.get("limit_per_source", 6)))

# -------------------- VOICE SETTINGS LOADER --------------------
def load_voice_settings(config_path: str = "voice_settings.json") -> dict:
    """
    Load ElevenLabs TTS settings from a JSON file.
    Expected structure:
    {
      "model_id": "eleven_multilingual_v2",
      "voice_settings": {
        "stability": 0.92,
        "similarity_boost": 0.86,
        "style": 0.10,
        "use_speaker_boost": true
      },
      "voice_speed": 1.06
    }
    Falls back to defaults if missing/invalid.
    """
    defaults = {
        "model_id": "eleven_v3",
        "voice_settings": {
            "stability": 0.80,
            "similarity_boost": 0.90,
            "style": 0.15,
            "use_speaker_boost": True
        },
        "voice_speed": 1.00
    }
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
        out = defaults.copy()
        out["model_id"] = cfg.get("model_id", out["model_id"])
        vs_in = cfg.get("voice_settings") or {}
        vs = out["voice_settings"].copy()
        # Merge recognized keys only
        for k in ("stability","similarity_boost","style","use_speaker_boost"):
            if k in vs_in:
                vs[k] = vs_in[k]
        out["voice_settings"] = vs
        if isinstance(cfg.get("voice_speed"), (int, float)):
            out["voice_speed"] = float(cfg["voice_speed"])
        return out
    except Exception as e:
        print(f"[diag] load_voice_settings fallback due to: {e}")
        return defaults

# -------------------- TIME / GREETING --------------------
def boston_now():
    now = dt.datetime.now(ZoneInfo("America/New_York"))
    hour = now.hour
    if 5 <= hour < 12:
        tod = "morning"
    elif 12 <= hour < 18:
        tod = "afternoon"
    else:
        tod = "evening"
    pretty_date = now.strftime("%A, %B ") + str(int(now.strftime("%d"))) + now.strftime(", %Y")
    return now, tod, pretty_date

# -------------------- FETCH / DEDUPE --------------------
def is_newsworthy(title: str) -> bool:
    t = (title or "").lower()
    return bool(t and not any(k in t for k in EXCLUDE))

def fetch_items():
    items = []
    for src in SOURCES:
        name = src.get("name","Unknown")
        rss  = src.get("rss","").strip()
        if not rss:
            continue
        try:
            fp = feedparser.parse(rss)
            count = 0
            for e in fp.entries:
                if count >= LIMIT_PER: break
                title = (e.get("title") or "").strip()
                link  = (e.get("link") or "").strip()
                if not title or not link: continue
                if not is_newsworthy(title): continue
                summary = (e.get("summary") or e.get("description") or "").strip()
                items.append({"source": name, "title": title, "link": link, "summary": summary})
                count += 1
        except Exception as ex:
            print(f"[warn] feed error {name}: {ex}", file=sys.stderr)
    return items

def dedupe(items, threshold=90):
    kept, seen = [], []
    for it in items:
        title = it["title"]
        if not seen:
            kept.append(it); seen.append(title); continue
        match = process.extractOne(title, seen, scorer=fuzz.token_set_ratio)
        if not match or match[1] < threshold:
            kept.append(it); seen.append(title)
    return kept

# -------------------- EXTRACTION --------------------
def extract_text(url: str) -> str:
    try:
        downloaded = trafilatura.fetch_url(url, timeout=25)
        if downloaded:
            extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if extracted and len(extracted.split()) > 40:
                return extracted
    except Exception:
        pass
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        doc = Document(r.text)
        text = BeautifulSoup(doc.summary(), "html.parser").get_text("\n")
        lines = [l.strip() for l in text.splitlines() if len(l.split()) > 4]
        return "\n".join(lines)
    except Exception:
        return ""

def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    for sep in [". ", " — ", " – ", " • "]:
        if sep in text:
            cand = text.split(sep)[0]
            if len(cand.split()) >= 8:
                return cand.strip(".•–— ")
    return text[:240].rsplit(" ",1)[0]

def build_notes(items):
    notes, used = [], 0
    for it in items:
        if used >= MAX_ITEMS: break
        txt = extract_text(it["link"])
        if not txt:
            txt = it.get("summary") or it["title"]
        sent = first_sentence(txt)
        if len(sent.split()) < 6:
            continue
        notes.append(f"{it['source']}: {sent}  (link: {it['link']})")
        used += 1
    return notes

# -------------------- OPENAI --------------------
try:
    from openai import OpenAI
    _client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    print(f"[warn] openai import failed: {e}", file=sys.stderr)
    _client = None

def rewrite_with_openai(prompt_text: str, notes: list[str]) -> str | None:
    # enhanced diag
    if not _client:
        print('[diag] OpenAI client not initialized. Check OPENAI_API_KEY.')
        return None
    if not OPENAI_MODEL:
        print('[diag] OPENAI_MODEL not set.'); return None
    now, tod, pretty_date = boston_now()
    sys_preamble = (
        "HARD CONSTRAINTS:\n"
        f"- Opening line MUST be: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Lead with the most important local news.\n"
        "- No editorializing.\n"
        "- Attribute sources naturally.\n"
        "- 5–8 items; smooth transitions; quick weather/events; beta disclosure.\n"
    )
    user_block = "STORIES (raw notes):\n" + "\n\n".join(notes)
    try:
        if OPENAI_MODEL.lower().startswith("gpt-5"):
            resp = _client.responses.create(
                model=OPENAI_MODEL,
                input=f"{sys_preamble}\n\n{prompt_text.strip()}\n\n{user_block}",
                max_output_tokens=1200,
            )
            return getattr(resp, "output_text", "").strip()
        else:
            resp = _client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role":"system","content":sys_preamble},
                    {"role":"user","content":f"{prompt_text.strip()}\n\n{user_block}"},
                ],
                temperature=0.35,
                max_tokens=1200,
            )
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[diag] OpenAI generation failed: {e}")
        return None

# -------------------- FALLBACK SCRIPT (no-OpenAI) --------------------
def build_script_fallback(notes: list[str]) -> str:
    now, tod, pretty_date = boston_now()
    greet = f"Good {tod}, it’s {pretty_date}."
    body_lines = []
    for i, n in enumerate(notes[:6], start=1):
        part = n.split("(link:")[0].strip()
        if i == 1:
            body_lines.append(part)
        else:
            trans = ["In other news,", "Meanwhile,", "Turning to", "Also today,", "Elsewhere in the city,"]
            body_lines.append(f"{trans[(i-2) % len(trans)]} {part}")
    closing = ("That’s the Boston Briefing. "
               "This script was written by AI and voiced using an AI clone of Matt Karolian’s voice. "
               "This is an internal beta — please do not share externally.")
    return " ".join([greet] + body_lines + [closing])

# -------------------- TTS SANITIZER --------------------
def sanitize_for_tts(s: str) -> str:
    rep = (("—", ", "), ("–", ", "), ("…", ". "), (" / ", " or "))
    for a,b in rep:
        s = s.replace(a,b)
    s = s.replace("MBTA", "M-B-T-A").replace("BPL", "B-P-L")
    return " ".join(s.split())

# -------------------- ELEVENLABS (SINGLE CLEAN REQUEST) --------------------
def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        print("[diag] Missing ELEVEN_API_KEY or ELEVEN_VOICE_ID or empty text.")
        return None
    base = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    url  = f"{base}?output_format=mp3_44100_128"
    cfg = load_voice_settings("voice_settings.json")
    print(f"[diag] ElevenLabs model={cfg.get('model_id')} speed={cfg.get('voice_speed')} "
          f"stability={cfg.get('voice_settings',{}).get('stability')} "
          f"style={cfg.get('voice_settings',{}).get('style')}")
    payload = {
        "text": text,
        "model_id": cfg.get("model_id", "eleven_multilingual_v2"),
        "voice_settings": cfg.get("voice_settings", {}),
        "voice_speed": cfg.get("voice_speed", 1.0)
    }
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
        if r.status_code >= 400:
            print(f"[diag] ElevenLabs HTTP {r.status_code}: {r.text[:200]}")
            return None
        return r.content
    except Exception as e:
        print(f"[diag] ElevenLabs error: {e}")
        return None

# -------------------- OUTPUT (SITE/FEED) --------------------
def write_shownotes(date_str, items):
    html = [f"<h2>Boston Briefing – {date_str}</h2>", "<ol>"]
    for it in items[:MAX_ITEMS]:
        html.append(f"<li><a href='{it['link']}'>{it['title']}</a> – {it['source']}</li>")
    html.append("</ol>")
    (SH_NOTES / f"{date_str}.html").write_text("\n".join(html), encoding="utf-8")

def write_index_if_missing():
    idx = PUBLIC_DIR / "index.html"
    if idx.exists():
        return
    url = f"{PUBLIC_BASE_URL}/feed.xml" if PUBLIC_BASE_URL else "feed.xml"
    html = f"<h1>Boston Briefing</h1><p>Podcast RSS: <a href='{url}'>{url}</a></p>"
    idx.write_text(html, encoding="utf-8")

def build_feed(episode_url: str, filesize: int):
    title = "Boston Briefing"
    desc  = "A short, factual Boston news briefing."
    now = dt.datetime.now().astimezone()
    last_build = format_datetime(now)
    item_title = now.strftime("Boston Briefing – %Y-%m-%d")
    guid = episode_url or item_title
    enclosure = f'<enclosure url="{episode_url}" length="{filesize}" type="audio/mpeg"/>' if episode_url else ""
    feed = [
        '<?xml version="1.0"?>',
        '<rss version="2.0"><channel>',
        f'<title>{title}</title><description>{desc}</description>',
        f'<lastBuildDate>{last_build}</lastBuildDate>',
        f'<item><title>{item_title}</title>{enclosure}</item>',
        '</channel></rss>'
    ]
    (PUBLIC_DIR / "feed.xml").write_text("\n".join(feed), encoding="utf-8")

# -------------------- MAIN --------------------
def main():
    # Gather news
    raw = fetch_items()
    deduped = dedupe(raw)
    notes = build_notes(deduped)

    # Prompt
    prompt_text = ""
    p = ROOT / "prompt.txt"
    if p.exists():
        prompt_text = p.read_text(encoding="utf-8")

    # Script (OpenAI or fallback)
    script = rewrite_with_openai(prompt_text, notes)
    if not script:
        print('[diag] Falling back to local script builder.')
        script = build_script_fallback(notes)
    script = sanitize_for_tts(script)

    # Outputs
    today = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = today.strftime("%Y-%m-%d")
    write_shownotes(date_str, deduped)
    write_index_if_missing()

    # TTS
    mp3_bytes = tts_elevenlabs(script)

    # Save audio + feed
    ep_url = ""
    filesize = 0
    ep_name = f"boston-briefing-{date_str}.mp3"
    ep_path = EP_DIR / ep_name
    if mp3_bytes:
        ep_path.write_bytes(mp3_bytes)
        filesize = len(mp3_bytes)
        ep_url = f"{PUBLIC_BASE_URL}/episodes/{ep_name}" if PUBLIC_BASE_URL else f"episodes/{ep_name}"
        print(f"[diag] saved MP3: {ep_path} ({filesize} bytes)")
    else:
        print("[diag] MP3 not created; continuing with feed + site")
    build_feed(ep_url, filesize)
    print("[diag] done.")

if __name__ == "__main__":
    main()
