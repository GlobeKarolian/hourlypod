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
    # “Monday, August 11, 2025” without %-d for Windows
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
    # 1) trafilatura first
    try:
        downloaded = trafilatura.fetch_url(url, timeout=25)
        if downloaded:
            extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if extracted and len(extracted.split()) > 40:
                return extracted
    except Exception:
        pass
    # 2) readability fallback
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent":"Mozilla/5.0 (compatible; BostonBriefing/1.0)"})
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
            # fall back to feed summary/title so we never end up empty
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
    """Responses for GPT-5 models, Chat for GPT-4o."""
    if not _client or not OPENAI_MODEL:
        print("[diag] OpenAI client/model missing")
        return None

    now, tod, pretty_date = boston_now()
    sys_preamble = (
        "HARD CONSTRAINTS:\n"
        f"- Opening line MUST be: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Lead with the most important local news; do not lead with sports unless it’s clearly the top story.\n"
        "- Absolutely no editorializing, sympathy, or sentiment.\n"
        "- Attribute sources naturally in-line (The Boston Globe, Boston.com, B-Side).\n"
        "- 5–8 items; smooth transitions; quick weather + notable events; end with beta disclosure.\n"
    )
    user_block = "STORIES (raw notes):\n" + "\n\n".join(notes)
    try:
        if OPENAI_MODEL.lower().startswith("gpt-5"):
            # Responses API—use max_output_tokens; omit temperature
            resp = _client.responses.create(
                model=OPENAI_MODEL,
                input=f"{sys_preamble}\n\n{prompt_text.strip()}\n\n{user_block}",
                max_output_tokens=1200,
            )
            txt = getattr(resp, "output_text", None)
            if txt and len(txt.split()) > 25:
                return txt.strip()
            # retry without cap if model ignores max_output_tokens
            resp2 = _client.responses.create(
                model=OPENAI_MODEL,
                input=f"{sys_preamble}\n\n{prompt_text.strip()}\n\n{user_block}",
            )
            return (getattr(resp2, "output_text", "") or "").strip()
        else:
            # Chat Completions (gpt-4o)
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
        print(f"[warn] OpenAI generation failed: {e}")
        # last-chance fallback to gpt-4o
        try:
            resp = _client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role":"system","content":sys_preamble},
                    {"role":"user","content":f"{prompt_text.strip()}\n\n{user_block}"},
                ],
                temperature=0.35,
                max_tokens=1200,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e2:
            print(f"[warn] OpenAI fallback failed: {e2}")
            return None

# -------------------- TTS SANITIZER --------------------
def sanitize_for_tts(s: str) -> str:
    # Normalize punctuation & expand acronyms to reduce prosody “drag”
    rep = (
        ("—", ", "), ("–", ", "), ("…", ". "),
        (" / ", " or "),
    )
    for a,b in rep:
        s = s.replace(a,b)
    # Expand a couple of Boston acronyms that get elongated
    s = s.replace("MBTA", "M-B-T-A").replace("BPL", "B-P-L")
    # Collapse whitespace
    return " ".join(s.split())

# -------------------- ELEVENLABS (NON-STREAMING, LATENCY OFF) --------------------
def _load_voice_settings():
    vs_json = ROOT / "voice_settings.json"
    if vs_json.exists():
        try:
            return json.loads(vs_json.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "stability": 0.72,
        "similarity_boost": 0.90,
        "style": 0.25,
        "use_speaker_boost": True,
        "voice_speed": 1.04,
        "model_id": "eleven_multilingual_v2"
    }

def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        print("[diag] skipping TTS; missing ELEVEN_API_KEY/VOICE_ID or empty text")
        return None

    settings = _load_voice_settings()
    base = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    # Force non-streaming + explicitly disable streaming latency + set output format
    url = f"{base}?optimize_streaming_latency=0&output_format=mp3_44100_128"
    print(f"[diag] ElevenLabs URL: {url}")

    payload = {
        "text": text,
        "model_id": settings.get("model_id", "eleven_multilingual_v2"),
        "voice_settings": {
            "stability": settings.get("stability", 0.72),
            "similarity_boost": settings.get("similarity_boost", 0.90),
            "style": settings.get("style", 0.25),
            "use_speaker_boost": bool(settings.get("use_speaker_boost", True))
        },
        "voice_speed": settings.get("voice_speed", 1.04)
    }
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }

    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=180)
    if r.status_code >= 400:
        print(f"[warn] ElevenLabs error {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    return r.content

# -------------------- OUTPUT (SITE/FEED) --------------------
def write_shownotes(date_str, items):
    html = ["<html><head><meta charset='utf-8'><title>Boston Briefing – Sources</title></head><body>"]
    html.append(f"<h2>Boston Briefing – {date_str}</h2>")
    html.append("<ol>")
    take = 0
    for it in items:
        if take >= MAX_ITEMS: break
        html.append(f"<li><a href='{it['link']}' target='_blank' rel='noopener'>{it['title']}</a> – {it['source']}</li>")
        take += 1
    html.append("</ol></body></html>")
    (SH_NOTES / f"{date_str}.html").write_text("\n".join(html), encoding="utf-8")

def write_index_if_missing():
    """Only scaffold if you haven't uploaded your own UI."""
    idx = PUBLIC_DIR / "index.html"
    if idx.exists():
        return
    url = f"{PUBLIC_BASE_URL}/feed.xml" if PUBLIC_BASE_URL else "feed.xml"
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Boston Briefing</title></head>
<body>
  <h1>Boston Briefing</h1>
  <p>Podcast RSS: <a href="{url}">{url}</a></p>
  <p>Shownotes: <a href="shownotes/">Open folder</a></p>
</body></html>"""
    idx.write_text(html, encoding="utf-8")

def build_feed(episode_url: str, filesize: int):
    title = "Boston Briefing"
    desc  = "A short, factual Boston news briefing."
    link  = PUBLIC_BASE_URL or ""
    now = dt.datetime.now().astimezone()
    last_build = format_datetime(now)
    item_title = now.strftime("Boston Briefing – %Y-%m-%d")
    guid = episode_url or item_title
    enclosure = f'<enclosure url="{episode_url}" length="{filesize}" type="audio/mpeg"/>' if episode_url else ""
    feed = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        '  <channel>',
        f'    <title>{title}</title>',
        f'    <link>{link}</link>',
        '    <language>en-us</language>',
        f'    <description>{desc}</description>',
        '    <itunes:author>Boston Briefing</itunes:author>',
        '    <itunes:explicit>false</itunes:explicit>',
        f'    <lastBuildDate>{last_build}</lastBuildDate>',
        '    <item>',
        f'      <title>{item_title}</title>',
        f'      <description>{desc}</description>',
        f'      <link>{episode_url}</link>',
        f'      <guid isPermaLink="false">{guid}</guid>',
        f'      <pubDate>{last_build}</pubDate>',
        f'      {enclosure}',
        '    </item>',
        '  </channel>',
        '</rss>',
        ''
    ]
    (PUBLIC_DIR / "feed.xml").write_text("\n".join(feed), encoding="utf-8")

# -------------------- MAIN --------------------
def main():
    print("[diag] starting run…")
    print(f"[diag] env: PUBLIC_BASE_URL=*** OPENAI_API_KEY=*** OPENAI_MODEL={OPENAI_MODEL} ELEVEN_API_KEY=*** ELEVEN_VOICE_ID=*** MAX_ITEMS={MAX_ITEMS}")

    raw = fetch_items()
    print(f"[diag] fetched from {len(SOURCES)} source(s); total entries={len(raw)}")
    deduped = dedupe(raw)
    print(f"[diag] total after dedupe: {len(deduped)}")

    notes = build_notes(deduped)
    print(f"[diag] notes built: {len(notes)}")

    # Load newsroom prompt (editable file)
    prompt_text = ""
    p = ROOT / "prompt.txt"
    if p.exists():
        prompt_text = p.read_text(encoding="utf-8")

    # Generate script
    script = None
    if prompt_text.strip() and notes:
        script = rewrite_with_openai(prompt_text, notes)

    # Fallback text if GPT failed/empty
    if not script or len(script.split()) < 25:
        script = ("Ooops, something went wrong. Sorry about that. "
                  "Why don't you email Matt Karolian so I can fix it.")

    # Sanitize for smoother TTS pacing
    script = sanitize_for_tts(script)

    print("\n--- SCRIPT TO READ ---\n")
    print(script.strip())
    print("\n--- END SCRIPT ---\n")

    # Output artifacts
    today = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = today.strftime("%Y-%m-%d")

    write_shownotes(date_str, deduped)
    write_index_if_missing()

    # TTS
    mp3_bytes = None
    try:
        mp3_bytes = tts_elevenlabs(script)
    except Exception as ex:
        print(f"[warn] ElevenLabs error: {ex}", file=sys.stderr)

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
