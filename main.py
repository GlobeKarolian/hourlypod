# main.py
import os, sys, json, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
from email.utils import format_datetime

import feedparser, requests, yaml
from bs4 import BeautifulSoup
from readability import Document
import trafilatura
from rapidfuzz import fuzz, process

# ---------------- Env / Paths ----------------
ELEVEN_API_KEY  = os.getenv("ELEVEN_API_KEY", "").strip()
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "").strip()

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o").strip()  # set to gpt-5 to use Responses API

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_ITEMS       = int(os.getenv("MAX_ITEMS", "12"))

PUBLIC_DIR = Path("public")
EP_DIR     = PUBLIC_DIR / "episodes"
SH_DIR     = PUBLIC_DIR / "shownotes"
for d in (PUBLIC_DIR, EP_DIR, SH_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---------------- Feeds ----------------
def load_feeds():
    with open("feeds.yml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    sources = cfg.get("sources", [])
    exclude = set(str(k).lower() for k in cfg.get("exclude_keywords", []))
    per_src = int(cfg.get("daily_limit_per_source") or cfg.get("daily_limit_per_source".replace("daily_", "")) or cfg.get("daily_limit_per_source", 6))
    return sources, exclude, per_src

def is_newsworthy(title: str, exclude):
    t = (title or "").lower()
    return t and not any(k in t for k in exclude)

def fetch_items(sources, exclude, per_src):
    items = []
    for src in sources:
        name, rss = src.get("name", "Unknown"), src.get("rss", "")
        if not rss:
            continue
        try:
            fp = feedparser.parse(rss)
            take = 0
            for e in fp.entries:
                if take >= per_src:
                    break
                title = (e.get("title") or "").strip()
                link  = (e.get("link") or "").strip()
                if not title or not link:
                    continue
                if not is_newsworthy(title, exclude):
                    continue
                items.append({"source": name, "title": title, "link": link})
                take += 1
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

# ---------------- Article text extraction ----------------
UA = {"User-Agent": "Mozilla/5.0 (BostonBriefing/1.0)"}

def extract_text(url: str) -> str:
    # 1) Trafilatura (handles a lot of paywall-lite templates, removes cruft)
    try:
        downloaded = trafilatura.fetch_url(url, timeout=20)
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if text and len(text.split()) > 40:
                return text
    except Exception:
        pass

    # 2) Readability fallback
    try:
        html = requests.get(url, timeout=20, headers=UA).text
        doc = Document(html)
        cleaned = doc.summary()
        text = BeautifulSoup(cleaned, "html.parser").get_text("\n")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        lines = [l for l in lines if len(l.split()) > 4]
        out = "\n".join(lines)
        if len(out.split()) > 40:
            return out
    except Exception:
        pass

    return ""

def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    for sep in [". ", " — ", " – ", ": "]:
        if sep in text:
            cand = text.split(sep)[0]
            if len(cand.split()) >= 8:
                return cand.strip(".•–—: ")
    # fallback: chop to ~220 chars
    return text[:220].rsplit(" ", 1)[0]

def build_notes(items):
    notes, used = [], 0
    for it in items:
        if used >= MAX_ITEMS:
            break
        txt = extract_text(it["link"])
        if not txt:
            continue
        sent = first_sentence(txt)
        if len(sent.split()) < 6:
            continue
        # Keep attributions light; the model will rewrite naturally
        notes.append(f"{it['source']}: {sent} (link: {it['link']})")
        used += 1
    return notes

# ---------------- Time helpers ----------------
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
def _openai_client():
    if not OPENAI_API_KEY:
        return None, None
    try:
        from openai import OpenAI
        return OpenAI(api_key=OPENAI_API_KEY), None
    except Exception as e:
        print(f"[warn] openai import failed: {e}", file=sys.stderr)
        return None, e

def _responses_api(client, prompt_text: str, notes: list[str], model: str) -> str:
    now, tod, pretty_date = boston_now()
    control = (
        "HARD CONSTRAINTS:\n"
        f"- Begin with: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Lead with the most important news (not sports unless it clearly dominates).\n"
        "- No editorializing, sympathy, or opinion. Just clear, neutral language.\n"
        "- Attribute naturally: 'The Globe reports…', 'Boston.com notes…', 'B-Side says…'.\n"
        "- Use 5–8 items, smooth transitions, quick weather + any notable local events near the end.\n"
        "- Close with the internal-beta disclosure.\n"
    )
    user_block = "STORY NOTES (raw, messy is ok):\n" + "\n\n".join(notes)
    full_input = f"{control}\n\nPROMPT:\n{prompt_text.strip()}\n\n{user_block}"

    # GPT-5 Responses API – do NOT send temperature (some versions reject it)
    resp = client.responses.create(
        model=model,
        input=full_input,
        max_output_tokens=1200,
    )
    return (getattr(resp, "output_text", None) or "").strip()

def _chat_api(client, prompt_text: str, notes: list[str], model: str) -> str:
    now, tod, pretty_date = boston_now()
    system = (
        "HARD CONSTRAINTS:\n"
        f"- Begin with: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Lead with the most important news (not sports unless it clearly dominates).\n"
        "- No editorializing, sympathy, or opinion.\n"
        "- Natural attributions to sources.\n"
        "- 5–8 items; smooth transitions; quick weather + notable events; internal-beta disclosure.\n"
    )
    user_block = "STORY NOTES (raw):\n" + "\n\n".join(notes)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"{prompt_text.strip()}\n\n{user_block}"},
        ],
        # Temperature is OK for chat models; keep it modest for stability
        temperature=0.3,
        max_tokens=1200,
    )
    return resp.choices[0].message.content.strip()

def rewrite_with_openai(prompt_text: str, notes: list[str]) -> str | None:
    client, err = _openai_client()
    if not client:
        return None
    try:
        if OPENAI_MODEL.lower().startswith("gpt-5"):
            return _responses_api(client, prompt_text, notes, OPENAI_MODEL)
        else:
            return _chat_api(client, prompt_text, notes, OPENAI_MODEL)
    except Exception as e:
        print(f"[warn] OpenAI primary model failed: {e}", file=sys.stderr)
        # Fallback to gpt-4o if available
        try:
            return _chat_api(client, prompt_text, notes, "gpt-4o")
        except Exception as e2:
            print(f"[warn] OpenAI fallback failed: {e2}", file=sys.stderr)
            return None

# ---------------- ElevenLabs TTS (optional) ----------------
def load_eleven_settings():
    """Read optional tuning from elevenlabs.json if present."""
    cfg_path = Path("elevenlabs.json")
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Safe defaults that tend to sound natural for news
    return {
        "model_id": "eleven_multilingual_v2",
        "voice_speed": 1.05,
        "voice_settings": {
            "stability": 0.55,
            "similarity_boost": 0.85,
            "style": 0.40,
            "use_speaker_boost": True
        }
    }

def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    payload = {"text": text}
    payload.update(load_eleven_settings())
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=120)
    r.raise_for_status()
    return r.content

# ---------------- Output (site + feed) ----------------
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
    (SH_DIR / f"{date_str}.html").write_text("\n".join(html), encoding="utf-8")

def write_index():
    feed_url = f"{PUBLIC_BASE_URL}/feed.xml" if PUBLIC_BASE_URL else "feed.xml"
    notes_url = f"{PUBLIC_BASE_URL}/shownotes/" if PUBLIC_BASE_URL else "shownotes/"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Boston Briefing (Internal Beta)</title></head>
<body>
<h1>Boston Briefing (Internal Beta)</h1>
<p><a href="{feed_url}">Podcast RSS</a></p>
<p><a href="{notes_url}">Show Notes</a></p>
</body></html>"""
    (PUBLIC_DIR / "index.html").write_text(html, encoding="utf-8")

def build_feed(episode_url: str, filesize: int):
    title = "Boston Briefing"
    desc  = "A short, factual Boston news briefing."
    link  = PUBLIC_BASE_URL or ""
    now_rfc = format_datetime(dt.datetime.now(ZoneInfo("America/New_York")))
    item_title = dt.datetime.now(ZoneInfo("America/New_York")).strftime("Boston Briefing – %Y-%m-%d")
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
        f'    <lastBuildDate>{now_rfc}</lastBuildDate>',
        '    <item>',
        f'      <title>{item_title}</title>',
        f'      <description>{desc}</description>',
        f'      <link>{episode_url}</link>',
        f'      <guid isPermaLink="false">{episode_url or item_title}</guid>',
        f'      <pubDate>{now_rfc}</pubDate>',
        f'      {enclosure}',
        '    </item>',
        '  </channel>',
        '</rss>',
        ''
    ]
    (PUBLIC_DIR / "feed.xml").write_text("\n".join(feed), encoding="utf-8")

# ---------------- Main ----------------
def main():
    # Load feeds + collect/prepare notes
    sources, exclude, per_src = load_feeds()
    raw_items = fetch_items(sources, exclude, per_src)
    items = dedupe(raw_items)
    notes = build_notes(items)

    # Load newsroom prompt
    prompt_text = ""
    p = Path("prompt.txt")
    if p.exists():
        prompt_text = p.read_text(encoding="utf-8")

    # Write script (or fallback)
    script = None
    if prompt_text and notes:
        script = rewrite_with_openai(prompt_text, notes)

    if not script or len(script.split()) < 30:
        script = ("Ooops, something went wrong. Sorry about that. "
                  "Why don't you email Matt Karolian so I can fix it.")

    # Log the script for quick review in Actions
    print("\n--- SCRIPT TO READ ---\n")
    print(script.strip())
    print("\n--- END SCRIPT ---\n")

    # Write site bits
    today = dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    write_shownotes(today, items)
    write_index()

    # TTS (optional) + feed
    mp3_bytes = None
    try:
        mp3_bytes = tts_elevenlabs(script)
    except Exception as ex:
        print(f"[warn] ElevenLabs error: {ex}", file=sys.stderr)

    ep_url = ""
    size = 0
    if mp3_bytes:
        name = f"boston-briefing-{today}.mp3"
        path = EP_DIR / name
        path.write_bytes(mp3_bytes)
        size = len(mp3_bytes)
        if PUBLIC_BASE_URL:
            ep_url = f"{PUBLIC_BASE_URL}/episodes/{name}"

    build_feed(ep_url, size)

if __name__ == "__main__":
    main()
