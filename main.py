# main.py
# Boston Briefing — RSS + GPT + ElevenLabs (with robust GPT-5 support)
# - Pulls/filters/dedupes items from feeds.yml
# - Extracts first-sentence notes
# - Rewrites to a natural script using prompt.txt
# - Generates MP3 via ElevenLabs
# - Publishes public/index.html, public/feed.xml, public/shownotes/<date>.html
# - Falls back to an apology audio if GPT/Eleven fails

import os, sys, json, datetime as dt
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml, requests, feedparser
from bs4 import BeautifulSoup
from readability import Document
import trafilatura
from rapidfuzz import fuzz, process

# ---------------- Env & paths ----------------
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

# ---------------- Load feeds ----------------
with open("feeds.yml", "r", encoding="utf-8") as f:
    feeds_cfg = yaml.safe_load(f) or {}
SOURCES      = feeds_cfg.get("sources", [])
EXCLUDE      = set(str(k).lower() for k in feeds_cfg.get("exclude_keywords", []))
LIMIT_PER    = int(feeds_cfg.get("daily_limit_per_source", feeds_cfg.get("daily_limit_per_source", 6)))

# ---------------- Helpers ----------------
def log(msg: str):
    print(f"[diag] {msg}")

def is_newsworthy(title: str) -> bool:
    t = (title or "").lower()
    return t and not any(k in t for k in EXCLUDE)

def fetch_items():
    items = []
    log(f"fetching from {len(SOURCES)} source(s), per_source cap={LIMIT_PER}")
    for src in SOURCES:
        name, rss = src.get("name","Unknown"), src.get("rss","")
        if not rss:
            continue
        try:
            fp = feedparser.parse(rss)
            raw = len(fp.entries or [])
            kept = 0
            for e in fp.entries:
                if kept >= LIMIT_PER: break
                title = (e.get("title") or "").strip()
                link  = (e.get("link") or "").strip()
                if not title or not link: continue
                if not is_newsworthy(title): continue
                items.append({"source": name, "title": title, "link": link})
                kept += 1
            log(f"{name}: {raw} raw entries")
            log(f"{name}: kept {kept}")
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

def extract_text(url: str) -> str:
    # Try trafilatura first
    try:
        downloaded = trafilatura.fetch_url(url, timeout=20)
        if downloaded:
            extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if extracted and len(extracted.split()) > 40:
                return extracted
    except Exception:
        pass
    # Fallback: readability
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
    for sep in [". ", " — ", " – ", ": "]:
        if sep in text:
            cand = text.split(sep)[0]
            if len(cand.split()) >= 8:
                return cand.strip(".•–—: ")
    return text[:240].rsplit(" ",1)[0]

def build_notes(items):
    notes = []
    used = 0
    log(f"total fetched (pre-dedupe): {len(items)}")
    items = dedupe(items)
    log(f"total after dedupe: {len(items)}")
    log(f"extracting up to MAX_ITEMS={MAX_ITEMS}")
    for it in items:
        if used >= MAX_ITEMS: break
        txt = extract_text(it["link"])
        if not txt:
            continue
        sent = first_sentence(txt)
        if len(sent.split()) < 6:
            continue
        notes.append(f"{it['source']}: {sent} (link: {it['link']})")
        log(f"[ok] {it['source']} — extracted")
        used += 1
    log(f"notes built: {len(notes)}")
    return notes

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

# ---------------- OpenAI ----------------
try:
    from openai import OpenAI
    _client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    print(f"[warn] openai import failed: {e}", file=sys.stderr)
    _client = None

def _use_responses_api(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith("gpt-5") or "responses" in m

def _responses_api(prompt_text: str, notes: list[str], model: str) -> str:
    now, tod, pretty_date = boston_now()
    control = (
        "HARD CONSTRAINTS (do not violate):\n"
        f"- Time-of-day greeting MUST be: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Lead with the most important news; do NOT lead with sports unless it is indisputably the top story.\n"
        "- Absolutely no editorializing, sympathy, or sentiment (no 'thoughts and prayers', 'we hope', etc.).\n"
        "- Integrate source names naturally in the flow (e.g., 'The Globe reports…', 'Boston.com says…', 'B-Side notes…').\n"
        "- 5–8 items; smooth, human transitions; quick weather + notable events; end with the internal beta disclosure.\n"
    )
    user_block = "STORIES (verbatim notes, may be messy):\n" + "\n\n".join(notes)
    full_input = f"{control}\n\nUSER PROMPT:\n{prompt_text.strip()}\n\n{user_block}"

    # Try with temperature first; if model rejects any param, retry minimal
    try:
        resp = _client.responses.create(
            model=model,
            input=full_input,
            temperature=0.35,
            max_completion_tokens=1200,   # <- Responses API expects this
        )
        return (getattr(resp, "output_text", None) or "").strip()
    except Exception as e:
        print(f"[warn] Responses API (with temperature) failed: {e}", file=sys.stderr)
        # Retry without temperature in case model disallows it
        resp = _client.responses.create(
            model=model,
            input=full_input,
            max_completion_tokens=1200,
        )
        return (getattr(resp, "output_text", None) or "").strip()

def _chat_api(prompt_text: str, notes: list[str], model: str) -> str:
    now, tod, pretty_date = boston_now()
    control = (
        "HARD CONSTRAINTS (do not violate):\n"
        f"- Time-of-day greeting MUST be: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Lead with the most important news; do NOT lead with sports unless it is indisputably the top story.\n"
        "- Absolutely no editorializing, sympathy, or sentiment (no 'thoughts and prayers', 'we hope', etc.).\n"
        "- Integrate source names naturally in the flow (e.g., 'The Globe reports…', 'Boston.com says…', 'B-Side notes…').\n"
        "- 5–8 items; smooth, human transitions; quick weather + notable events; end with the internal beta disclosure.\n"
    )
    user_block = "STORIES (verbatim notes, may be messy):\n" + "\n\n".join(notes)
    resp = _client.chat.completions.create(
        model=model,
        messages=[
            {"role":"system","content":control},
            {"role":"user","content":f"{prompt_text.strip()}\n\n{user_block}"},
        ],
        temperature=0.35,
        max_tokens=1200,  # Chat Completions expects max_tokens
    )
    return resp.choices[0].message.content.strip()

def rewrite_with_openai(prompt_text: str, notes: list[str]) -> str | None:
    if not _client or not OPENAI_MODEL:
        return None
    try:
        if _use_responses_api(OPENAI_MODEL):
            out = _responses_api(prompt_text, notes, OPENAI_MODEL)
        else:
            out = _chat_api(prompt_text, notes, OPENAI_MODEL)
        return (out or "").strip()
    except Exception as e:
        print(f"[warn] OpenAI generation failed: {e}", file=sys.stderr)
        # Fallback to gpt-4o via Chat Completions, which is very permissive
        try:
            out = _chat_api(prompt_text, notes, "gpt-4o")
            return (out or "").strip()
        except Exception as e2:
            print(f"[warn] OpenAI fallback failed: {e2}", file=sys.stderr)
            return None

# ---------------- ElevenLabs ----------------
def load_voice_settings() -> dict:
    # Optional JSON file for fine-tuning; otherwise sensible defaults
    vs = {
        "stability": 0.55,
        "similarity_boost": 0.85,
        "style": 0.40,
        "use_speaker_boost": True,
        "voice_speed": 1.05,
        "model_id": "eleven_multilingual_v2",
    }
    try:
        p = Path("voice_settings.json")
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                vs.update(data)
    except Exception as e:
        print(f"[warn] voice_settings.json load failed: {e}", file=sys.stderr)
    return vs

def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    vs = load_voice_settings()
    payload = {
        "text": text,
        "voice_settings": {
            "stability": vs.get("stability", 0.55),
            "similarity_boost": vs.get("similarity_boost", 0.85),
            "style": vs.get("style", 0.40),
            "use_speaker_boost": vs.get("use_speaker_boost", True),
        },
        "voice_speed": vs.get("voice_speed", 1.05),
        "model_id": vs.get("model_id", "eleven_multilingual_v2"),
    }
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=120)
    r.raise_for_status()
    return r.content

# ---------------- Output ----------------
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

def write_index():
    url = f"{PUBLIC_BASE_URL}/feed.xml" if PUBLIC_BASE_URL else "feed.xml"
    notes_url = f"{(PUBLIC_BASE_URL or '.').rstrip('/')}/shownotes/"
    html = f"""<html><head><meta charset='utf-8'><title>Boston Briefing (Internal Beta)</title></head>
<body>
  <h1>Boston Briefing (Internal Beta)</h1>
  <p><a href="{url}">Podcast RSS</a></p>
  <p><a href="{notes_url}">Show Notes</a></p>
</body></html>"""
    (PUBLIC_DIR / "index.html").write_text(html, encoding="utf-8")

def build_feed(episode_url: str, filesize: int):
    title = "Boston Briefing"
    desc  = "A short, factual Boston news briefing."
    link  = PUBLIC_BASE_URL or ""
    now   = dt.datetime.now().astimezone()
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

# ---------------- Main ----------------
def main():
    print("== Run python main.py")
    print("python main.py")
    print("shell: /usr/bin/bash -e {0}")
    print("env:")
    print(f"  PUBLIC_BASE_URL: {'***' if PUBLIC_BASE_URL else ''}")
    print(f"  OPENAI_API_KEY: {'***' if OPENAI_API_KEY else ''}")
    print(f"  OPENAI_MODEL: {'***' if OPENAI_MODEL else ''}")
    print(f"  ELEVEN_API_KEY: {'***' if ELEVEN_API_KEY else ''}")
    print(f"  ELEVEN_VOICE_ID: {'***' if ELEVEN_VOICE_ID else ''}")
    print(f"  MAX_ITEMS: {MAX_ITEMS}")

    # 1) Fetch + extract notes
    items = fetch_items()
    notes = build_notes(items)

    # 2) Load newsroom prompt (editable)
    prompt_text = ""
    ptxt = Path("prompt.txt")
    if ptxt.exists():
        prompt_text = ptxt.read_text(encoding="utf-8")

    # 3) Try GPT rewrite
    gpt_script = None
    if prompt_text and notes and OPENAI_API_KEY:
        gpt_script = rewrite_with_openai(prompt_text, notes)
        if not gpt_script:
            print("[warn] GPT rewrite returned empty")

    # 4) Fallback script if GPT failed/empty
    if gpt_script and len(gpt_script.split()) > 20:
        final_script = gpt_script
        log("OpenAI client ready; model=****")
    else:
        final_script = (
            "Ooops, something went wrong. Sorry about that. "
            "Why don't you email Matt Karolian so I can fix it."
        )

    # 5) Log the script
    print("\n--- SCRIPT TO READ ---\n")
    print(final_script.strip())
    print("\n--- END SCRIPT ---\n")

    # 6) Write site scaffolding
    today = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = today.strftime("%Y-%m-%d")
    write_shownotes(date_str, items)
    write_index()

    # 7) TTS + save MP3
    mp3_bytes = None
    try:
        mp3_bytes = tts_elevenlabs(final_script)
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
        log(f"saved MP3: public/episodes/{ep_name} ({filesize} bytes)")
    else:
        log("no MP3 generated")

    # 8) RSS feed
    build_feed(ep_url, filesize)
    log("done.")

if __name__ == "__main__":
    main()
