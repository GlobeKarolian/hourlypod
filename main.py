import os, sys, json, datetime as dt
from pathlib import Path
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import yaml, feedparser, requests
from bs4 import BeautifulSoup
from readability import Document
import trafilatura
from rapidfuzz import fuzz, process

# -------------------------
# Config (env or defaults)
# -------------------------
ELEVEN_API_KEY  = os.getenv("ELEVEN_API_KEY", "").strip()
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o").strip()
MAX_ITEMS       = int(os.getenv("MAX_ITEMS", "10"))

PUBLIC_DIR = Path("public")
EP_DIR     = PUBLIC_DIR / "episodes"
SH_NOTES   = PUBLIC_DIR / "shownotes"
for d in (PUBLIC_DIR, EP_DIR, SH_NOTES):
    d.mkdir(parents=True, exist_ok=True)

# -------------------------
# Load feeds.yml (RSS only)
# -------------------------
with open("feeds.yml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
SOURCES     = cfg.get("sources", [])
EXCLUDE_KWS = set(str(k).lower() for k in cfg.get("exclude_keywords", []))
LIMIT_PER   = int(cfg.get("daily_limit_per_source", cfg.get("limit_per_source", 6)))

def is_newsworthy(title: str) -> bool:
    t = (title or "").lower()
    return bool(t) and not any(k in t for k in EXCLUDE_KWS)

def fetch_rss_items():
    items = []
    for src in SOURCES:
        name = src.get("name", "Unknown")
        rss  = src.get("rss", "")
        if not rss:
            continue
        try:
            fp = feedparser.parse(rss)
            count = 0
            for e in fp.entries:
                if count >= LIMIT_PER: break
                title = (e.get("title") or "").strip()
                link  = (e.get("link")  or "").strip()
                if not title or not link: 
                    continue
                if not is_newsworthy(title):
                    continue
                items.append({"source": name, "title": title, "link": link})
                count += 1
        except Exception as ex:
            print(f"[warn] RSS error {name}: {ex}", file=sys.stderr)
    print(f"[diag] fetched from {len(SOURCES)} source(s); total entries={len(items)}")
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
    print(f"[diag] total after dedupe: {len(kept)}")
    return kept

# -------------------------
# Article extraction
# -------------------------
def extract_text(url: str) -> str:
    # 1) Trafilatura
    try:
        downloaded = trafilatura.fetch_url(url, timeout=20)
        if downloaded:
            extracted = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                target_language="en"
            )
            if extracted and len(extracted.split()) > 40:
                return extracted
    except Exception:
        pass
    # 2) Readability
    try:
        html = requests.get(url, timeout=20, headers={"User-Agent":"Mozilla/5.0"}).text
        doc = Document(html)
        cleaned = doc.summary()
        text = BeautifulSoup(cleaned, "html.parser").get_text("\n")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        lines = [l for l in lines if len(l.split()) > 4]
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
    return text[:240].rsplit(" ", 1)[0]

def build_notes(items):
    notes = []
    used = 0
    for it in items:
        if used >= MAX_ITEMS: break
        txt = extract_text(it["link"])
        if not txt:
            continue
        sent = first_sentence(txt)
        if len(sent.split()) < 6:
            continue
        notes.append(f"{it['source']}: {sent}  (link: {it['link']})")
        used += 1
    print(f"[diag] notes built: {len(notes)}")
    return notes

# -------------------------
# Time / Greeting
# -------------------------
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

# -------------------------
# OpenAI client
# -------------------------
try:
    from openai import OpenAI
    _client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    print(f"[warn] openai import failed: {e}", file=sys.stderr)
    _client = None

def rewrite_with_openai(prompt_text: str, notes: list[str]) -> str | None:
    if not _client or not OPENAI_MODEL:
        print("[diag] OpenAI disabled or model missing.")
        return None

    now, tod, pretty_date = boston_now()
    guardrails = (
        "HARD CONSTRAINTS (do not violate):\n"
        f"- Opening line must be: \"Good {tod}, it’s {pretty_date}.\"\n"
        "- Lead with the most important news; never lead with sports unless it’s indisputably top.\n"
        "- No editorializing or sympathy; keep it factual and concise.\n"
        "- Attribute sources naturally in-line (The Globe reports…, Boston.com says…, B-Side notes…).\n"
        "- 5–8 items; smooth transitions; quick weather + notable events; then disclosure.\n"
    )
    user_block = "STORIES (notes; rough, may have duplicates):\n" + "\n\n".join(notes)
    full_input = f"{guardrails}\n\nUSER PROMPT:\n{prompt_text.strip()}\n\n{user_block}"

    try:
        if OPENAI_MODEL.lower().startswith("gpt-5"):
            # Responses API (avoid invalid params)
            resp = _client.responses.create(
                model=OPENAI_MODEL,
                input=full_input,
                max_output_tokens=1200,   # valid param for Responses API
            )
            out = getattr(resp, "output_text", None)
            if out:
                return out.strip()
            return None
        else:
            # Chat Completions for 4o / others
            resp = _client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role":"system","content":guardrails},
                    {"role":"user","content":full_input},
                ],
                max_tokens=1200
            )
            return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[warn] OpenAI error: {e}", file=sys.stderr)
        # try a quiet fallback to 4o
        try:
            resp = _client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role":"system","content":guardrails},
                    {"role":"user","content":full_input},
                ],
                max_tokens=1200
            )
            return resp.choices[0].message.content.strip()
        except Exception as e2:
            print(f"[warn] OpenAI fallback failed: {e2}", file=sys.stderr)
            return None

# -------------------------
# ElevenLabs TTS
# -------------------------
def load_voice_settings() -> dict:
    p = Path("voice_settings.json")
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    # sensible natural defaults
    return {
        "stability": 0.62,
        "similarity_boost": 0.9,
        "style": 0.28,
        "use_speaker_boost": True,
        "voice_speed": 0.98
    }

def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    vs = load_voice_settings()
    payload = {
        "text": text,
        "voice_settings": {
            "stability": vs.get("stability", 0.62),
            "similarity_boost": vs.get("similarity_boost", 0.9),
            "style": vs.get("style", 0.28),
            "use_speaker_boost": bool(vs.get("use_speaker_boost", True)),
        },
        "voice_speed": vs.get("voice_speed", 0.98),
        "model_id": "eleven_multilingual_v2"
    }
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=180)
    if r.status_code >= 400:
        print(f"[warn] ElevenLabs HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    return r.content

# -------------------------
# Site output helpers
# -------------------------
def write_index():
    url = f"{PUBLIC_BASE_URL}/feed.xml" if PUBLIC_BASE_URL else "feed.xml"
    html = f"""<html><head><meta charset='utf-8'><title>Boston Briefing (Internal Beta)</title></head>
<body>
  <h1>Boston Briefing (Internal Beta)</h1>
  <p><a href="{url}">Podcast RSS</a></p>
  <p><a href="{(PUBLIC_BASE_URL or '.').rstrip('/')}/shownotes/">Show Notes</a></p>
</body></html>"""
    (PUBLIC_DIR / "index.html").write_text(html, encoding="utf-8")

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

def build_feed(episode_url: str, filesize: int):
    title = "Boston Briefing"
    desc  = "A short, factual Boston news briefing."
    link  = PUBLIC_BASE_URL or ""
    last_build = dt.datetime.now().astimezone()
    item_title = last_build.strftime("Boston Briefing – %Y-%m-%d")
    enclosure = f'<enclosure url="{episode_url}" length="{filesize}" type="audio/mpeg"/>' if episode_url else ""
    rss = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        '  <channel>',
        f'    <title>{title}</title>',
        f'    <link>{link}</link>',
        '    <language>en-us</language>',
        f'    <description>{desc}</description>',
        '    <itunes:author>Boston Briefing</itunes:author>',
        '    <itunes:explicit>false</itunes:explicit>',
        f'    <lastBuildDate>{format_datetime(last_build)}</lastBuildDate>',
        '    <item>',
        f'      <title>{item_title}</title>',
        f'      <description>{desc}</description>',
        f'      <link>{episode_url}</link>',
        f'      <guid isPermaLink="false">{episode_url or item_title}</guid>',
        f'      <pubDate>{format_datetime(last_build)}</pubDate>',
        f'      {enclosure}',
        '    </item>',
        '  </channel>',
        '</rss>',
        ''
    ]
    (PUBLIC_DIR / "feed.xml").write_text("\n".join(rss), encoding="utf-8")

# -------------------------
# Main
# -------------------------
def main():
    print("[diag] starting run…")
    # 1) RSS → dedupe → extract → notes
    items = dedupe(fetch_rss_items())
    notes = build_notes(items)

    # 2) Prompt
    prompt_path = Path("prompt.txt")
    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""

    # 3) Generate script via OpenAI (RSS-only)
    script = None
    if prompt_text and notes:
        script = rewrite_with_openai(prompt_text, notes)

    # 4) Fallback message if empty
    if not script or len(script.split()) < 25:
        script = (
            "Ooops, something went wrong. Sorry about that. "
            "Why don't you email Matt Karolian so I can fix it."
        )

    print("\n--- SCRIPT TO READ ---\n")
    print(script.strip())
    print("\n--- END SCRIPT ---\n")

    # 5) Output pages
    now = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = now.strftime("%Y-%m-%d")
    write_shownotes(date_str, items)
    write_index()

    # 6) TTS + feed
    mp3_bytes = None
    try:
        mp3_bytes = tts_elevenlabs(script)
    except Exception as ex:
        print(f"[warn] ElevenLabs error: {ex}", file=sys.stderr)

    ep_url = ""
    filesize = 0
    if mp3_bytes:
        ep_name = f"boston-briefing-{date_str}.mp3"
        ep_path = EP_DIR / ep_name
        ep_path.write_bytes(mp3_bytes)
        filesize = len(mp3_bytes)
        if PUBLIC_BASE_URL:
            ep_url = f"{PUBLIC_BASE_URL}/episodes/{ep_name}"

    build_feed(ep_url, filesize)
    print(f"[diag] saved MP3: {('episodes/' + ep_name) if mp3_bytes else 'none'}")

if __name__ == "__main__":
    main()
