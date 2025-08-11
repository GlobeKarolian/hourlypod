import os, sys, json, datetime as dt
import feedparser, requests, yaml
from bs4 import BeautifulSoup
from readability import Document
import trafilatura
from rapidfuzz import fuzz, process
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------- Config ----------
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

# ---------- Load feeds ----------
with open("feeds.yml", "r", encoding="utf-8") as f:
    feeds_cfg = yaml.safe_load(f) or {}
SOURCES    = feeds_cfg.get("sources", [])
EXCLUDE    = set(str(k).lower() for k in feeds_cfg.get("exclude_keywords", []))
LIMIT_PER  = int(feeds_cfg.get("daily_limit_per_source", 6))

# ---------- Helpers ----------
def is_newsworthy(title: str) -> bool:
    t = (title or "").lower()
    return t and not any(k in t for k in EXCLUDE)

def fetch_items():
    items = []
    for src in SOURCES:
        name, rss = src.get("name","Unknown"), src.get("rss","")
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
                items.append({"source": name, "title": title, "link": link})
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

def extract_text(url: str) -> str:
    try:
        downloaded = trafilatura.fetch_url(url, timeout=20)
        if downloaded:
            extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if extracted and len(extracted.split()) > 40:
                return extracted
    except Exception:
        pass
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
    return text[:240].rsplit(" ",1)[0]

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

# ---------- OpenAI ----------
try:
    from openai import OpenAI
    _client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    print(f"[warn] openai import failed: {e}", file=sys.stderr)
    _client = None

def rewrite_with_openai(prompt_text: str, notes: list[str]) -> str | None:
    if not _client or not OPENAI_MODEL:
        return None
    now, tod, pretty_date = boston_now()
    control = (
        f"HARD CONSTRAINTS:\n"
        f"- Start with: 'Good {tod}, it’s {pretty_date}.'\n"
        f"- Cover top Boston-area stories from provided notes only.\n"
        f"- No editorializing. Use neutral, factual tone.\n"
        f"- Mention sources naturally in sentences.\n"
        f"- End with quick weather + notable events, then disclosure.\n"
    )
    user_block = "STORIES:\n" + "\n\n".join(notes)
    try:
        resp = _client.responses.create(
            model=OPENAI_MODEL,
            input=f"{control}\n\nPrompt:\n{prompt_text.strip()}\n\n{user_block}",
            max_output_tokens=1200
        )
        return (getattr(resp, "output_text", None) or "").strip()
    except Exception as e:
        print(f"[warn] OpenAI error: {e}", file=sys.stderr)
        return None

# ---------- ElevenLabs ----------
def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    payload = {
        "text": text,
        "voice_settings": {
            "stability": 0.65,
            "similarity_boost": 0.92,
            "style": 0.55,
            "use_speaker_boost": True
        },
        "model_id": "eleven_multilingual_v2",
        "voice_speed": 1.0
    }
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=300)
    r.raise_for_status()
    return r.content

# ---------- Output ----------
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
    html = f"""<html><head><meta charset='utf-8'><title>Boston Briefing</title></head>
<body>
  <h1>Boston Briefing</h1>
  <p>Podcast RSS: <a href="{url}">{url}</a></p>
  <p>Shownotes: <a href="{(PUBLIC_BASE_URL or '.').rstrip('/')}/shownotes/">Open folder</a></p>
</body></html>"""
    (PUBLIC_DIR / "index.html").write_text(html, encoding="utf-8")

def build_feed(episode_url: str, filesize: int):
    title = "Boston Briefing"
    desc  = "A short, factual Boston news briefing."
    link  = PUBLIC_BASE_URL or ""
    last_build = dt.datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
    item_title = dt.datetime.now().strftime("Boston Briefing – %Y-%m-%d")
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

# ---------- Main ----------
def main():
    items = dedupe(fetch_items())
    notes = build_notes(items)
    prompt_text = Path("prompt.txt").read_text(encoding="utf-8") if Path("prompt.txt").exists() else ""
    gpt_script = None
    if prompt_text and notes:
        gpt_script = rewrite_with_openai(prompt_text, notes)

    if gpt_script and len(gpt_script.split()) > 20:
        final_script = gpt_script
    else:
        final_script = "Ooops, something went wrong. Sorry about that. Why don't you email Matt Karolian so I can fix it."

    print("\n--- SCRIPT TO READ ---\n")
    print(final_script.strip())
    print("\n--- END SCRIPT ---\n")

    today = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = today.strftime("%Y-%m-%d")
    write_shownotes(date_str, items)
    write_index()

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

    build_feed(ep_url, filesize)

if __name__ == "__main__":
    main()
