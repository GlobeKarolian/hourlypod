#!/usr/bin/env python3
import os, sys, json, datetime as dt
from pathlib import Path
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import yaml, feedparser, requests
from bs4 import BeautifulSoup
from readability import Document
import trafilatura
from rapidfuzz import fuzz, process

# =========================
# Environment / constants
# =========================
ELEVEN_API_KEY   = os.getenv("ELEVEN_API_KEY", "").strip()
ELEVEN_VOICE_ID  = os.getenv("ELEVEN_VOICE_ID", "").strip()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "").strip()  # e.g., "gpt-5" or "gpt-4o"
PUBLIC_BASE_URL  = os.getenv("PUBLIC_BASE_URL", "").strip()
MAX_ITEMS        = int(os.getenv("MAX_ITEMS", "10"))

# Output dirs
PUBLIC_DIR = Path("public")
EP_DIR     = PUBLIC_DIR / "episodes"
SH_NOTES   = PUBLIC_DIR / "shownotes"
for d in (PUBLIC_DIR, EP_DIR, SH_NOTES):
    d.mkdir(parents=True, exist_ok=True)

# =========================
# Utility: time + greeting
# =========================
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

# =========================
# Load feeds.yml
# =========================
def load_feeds():
    cfg_path = Path("feeds.yml")
    if not cfg_path.exists():
        print("[err] feeds.yml not found", file=sys.stderr)
        return dict(sources=[], exclude_keywords=[], daily_limit_per_source=6)
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    data.setdefault("sources", [])
    data.setdefault("exclude_keywords", [])
    data.setdefault("daily_limit_per_source", 6)
    return data

# =========================
# Feed fetching / parsing
# =========================
def is_newsworthy(title: str, exclude_keywords):
    t = (title or "").lower()
    return bool(t) and not any(k.lower() in t for k in exclude_keywords)

def fetch_items(feeds_cfg):
    sources = feeds_cfg.get("sources", [])
    exclude = feeds_cfg.get("exclude_keywords", [])
    limit   = int(feeds_cfg.get("daily_limit_per_source", 6))

    all_items = []
    print(f"[diag] fetching from {len(sources)} source(s), per-source cap={limit}")
    for src in sources:
        name = src.get("name", "Unknown")
        rss  = src.get("rss", "")
        if not rss:
            print(f"[warn] missing RSS for {name}")
            continue
        try:
            fp = feedparser.parse(rss)
            if fp.bozo:
                print(f"[warn] feedparser flagged bozo for {name}: {getattr(fp, 'bozo_exception', '')}")
            entries = getattr(fp, "entries", []) or []
            print(f"[diag] {name}: {len(entries)} raw entries")
            used = 0
            for e in entries:
                if used >= limit: break
                title = (e.get("title") or "").strip()
                link  = (e.get("link") or "").strip()
                if not title or not link:
                    print(f"  [skip] empty title/link from {name}")
                    continue
                if not is_newsworthy(title, exclude):
                    print(f"  [skip] excluded by keyword: {title}")
                    continue
                all_items.append({"source": name, "title": title, "link": link})
                used += 1
            print(f"[diag] {name}: kept {used}")
        except Exception as ex:
            print(f"[warn] feed error {name}: {ex}", file=sys.stderr)
    print(f"[diag] total fetched (pre-dedupe): {len(all_items)}")
    return all_items

def dedupe(items, threshold=90):
    kept, seen = [], []
    for it in items:
        title = it["title"]
        if not seen:
            kept.append(it); seen.append(title); continue
        match = process.extractOne(title, seen, scorer=fuzz.token_set_ratio)
        if not match or (match and match[1] < threshold):
            kept.append(it); seen.append(title)
    print(f"[diag] total after dedupe: {len(kept)}")
    return kept

# =========================
# Extraction helpers
# =========================
def extract_text(url: str) -> str:
    # Try trafilatura first
    try:
        downloaded = trafilatura.fetch_url(url, timeout=20)
        if downloaded:
            extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if extracted and len(extracted.split()) > 40:
                return extracted
    except Exception as e:
        pass

    # Fallback to readability
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
    # fallback: first ~240 chars
    return text[:240].rsplit(" ",1)[0]

def build_notes(items):
    """
    Build short notes; try full-text extraction. If extraction fails,
    fall back to using the headline + link (so GPT can still write).
    """
    notes = []
    used = 0
    print(f"[diag] extracting up to MAX_ITEMS={MAX_ITEMS}")
    for it in items:
        if used >= MAX_ITEMS: break
        url = it["link"]
        title = it["title"]
        body = extract_text(url)
        if body:
            sent = first_sentence(body)
            if len(sent.split()) >= 6:
                notes.append(f"{it['source']}: {sent}  (link: {url})")
                used += 1
                print(f"  [ok] {it['source']} – extracted")
                continue
        # Headline fallback
        notes.append(f"{it['source']}: {title}  (link: {url})")
        used += 1
        print(f"  [fallback] {it['source']} – using headline only")
    print(f"[diag] notes built: {len(notes)}")
    return notes

# =========================
# OpenAI (Responses or Chat)
# =========================
_client = None
def init_openai():
    global _client
    if not OPENAI_API_KEY:
        print("[warn] OPENAI_API_KEY missing")
        return
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
        print(f"[diag] OpenAI client ready; model={OPENAI_MODEL or '(default)'}")
    except Exception as e:
        print(f"[warn] openai import/init failed: {e}", file=sys.stderr)

def rewrite_script(prompt_text: str, notes: list[str]) -> str | None:
    if not _client:
        return None
    now, tod, pretty_date = boston_now()
    control = (
        "HARD CONSTRAINTS:\n"
        f"- Opening line MUST be: \"Good {tod}, it’s {pretty_date}.\" (exact time-of-day & date).\n"
        "- Lead with the most important news; do NOT lead with sports unless it is unquestionably top.\n"
        "- Strictly no editorializing or sympathy. No ‘thoughts and prayers,’ ‘we hope,’ etc.\n"
        "- Attribute naturally (e.g., “The Globe reports…”, “Boston.com notes…”, “B-Side says…”).\n"
        "- Aim for 5–8 items with smooth, natural radio-friendly transitions.\n"
        "- Close with a 1–2 sentence Boston weather summary and 1–2 notable events.\n"
        "- End with this disclosure: “This is part of an internal beta. All stories were summarized by AI, "
        "the script was AI-written, and the voice is an AI recreation of Matt Karolian’s voice. Please do not share externally.”\n"
    )
    user_block = "STORIES (verbatim notes; messy is okay):\n" + "\n\n".join(notes)
    try:
        # Prefer Responses API for GPT-5 style models or anything starting with "gpt-5"
        use_responses = (OPENAI_MODEL.lower().startswith("gpt-5") if OPENAI_MODEL else False)
        if use_responses:
            # Keep the param surface minimal (temperature omitted to avoid 400s on some models)
            resp = _client.responses.create(
                model=OPENAI_MODEL,
                input=f"{control}\n\nUSER PROMPT:\n{prompt_text.strip()}\n\n{user_block}",
                max_completion_tokens=1400,
            )
            text = (getattr(resp, "output_text", None) or "").strip()
            if text:
                return text
        # Fallback to Chat Completions (good for gpt-4o)
        model = OPENAI_MODEL or "gpt-4o"
        resp = _client.chat.completions.create(
            model=model,
            messages=[
                {"role":"system","content":control},
                {"role":"user","content":f"{prompt_text.strip()}\n\n{user_block}"},
            ],
            temperature=0.3,
            max_tokens=1400,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[warn] OpenAI generation failed: {e}", file=sys.stderr)
        return None

# =========================
# ElevenLabs TTS
# =========================
def load_voice_settings():
    # 1) voice_settings.json (highest priority)
    js = Path("voice_settings.json")
    if js.exists():
        try:
            data = json.loads(js.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # 2) elevenlabs_config.py (optional)
    try:
        import importlib
        cfg = importlib.import_module("elevenlabs_config")
        maybe = getattr(cfg, "VOICE_SETTINGS", None)
        if isinstance(maybe, dict):
            return maybe
    except Exception:
        pass
    # 3) sensible defaults
    return {
        "stability": 0.55,
        "similarity_boost": 0.85,
        "style": 0.40,
        "use_speaker_boost": True,
        "voice_speed": 1.05,     # top-level convenience; we’ll map below
        "model_id": "eleven_multilingual_v2",
    }

def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        print("[warn] ElevenLabs missing key/voice or empty text — skipping TTS")
        return None
    vs = load_voice_settings()
    payload = {
        "text": text,
        "voice_settings": {
            "stability":         float(vs.get("stability", 0.55)),
            "similarity_boost":  float(vs.get("similarity_boost", 0.85)),
            "style":             float(vs.get("style", 0.40)),
            "use_speaker_boost": bool(vs.get("use_speaker_boost", True)),
        },
        "model_id": vs.get("model_id", "eleven_multilingual_v2"),
    }
    # Some APIs accept pacing param separately; if provided, pass it as top-level:
    if "voice_speed" in vs:
        payload["voice_speed"] = float(vs["voice_speed"])
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    headers = {"xi-api-key": ELEVEN_API_KEY, "accept": "audio/mpeg", "content-type": "application/json"}
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=120)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"[warn] ElevenLabs error: {e}", file=sys.stderr)
        return None

# =========================
# HTML / Feed output
# =========================
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
    html = f"""<html><head><meta charset='utf-8'><title>Boston Briefing (Internal Beta)</title></head>
<body>
  <h1>Boston Briefing (Internal Beta)</h1>
  <p><a href="{url}">Podcast RSS</a></p>
  <p><a href="{(PUBLIC_BASE_URL or '.').rstrip('/')}/shownotes/">Show Notes</a></p>
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

# =========================
# Main orchestration
# =========================
def main():
    print("[diag] starting run…")
    print(f"[diag] env: PUBLIC_BASE_URL={'***' if PUBLIC_BASE_URL else '(unset)'} "
          f"OPENAI_API_KEY={'***' if OPENAI_API_KEY else '(unset)'} "
          f"OPENAI_MODEL={'***' if OPENAI_MODEL else '(unset)'} "
          f"ELEVEN_API_KEY={'***' if ELEVEN_API_KEY else '(unset)'} "
          f"ELEVEN_VOICE_ID={'***' if ELEVEN_VOICE_ID else '(unset)'} "
          f"MAX_ITEMS={MAX_ITEMS}")

    feeds_cfg = load_feeds()
    items_raw = fetch_items(feeds_cfg)
    items = dedupe(items_raw)

    notes = build_notes(items)

    # Write index + shownotes regardless (so GH Pages always updates)
    today = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = today.strftime("%Y-%m-%d")
    write_shownotes(date_str, items)
    write_index()

    prompt_text = Path("prompt.txt").read_text(encoding="utf-8") if Path("prompt.txt").exists() else ""
    init_openai()

    script = None
    if prompt_text and notes and _client:
        script = rewrite_script(prompt_text, notes)

    if not script or len(script.split()) < 30:
        # Fallback
        script = "Ooops, something went wrong. Sorry about that. Why don't you email Matt Karolian so I can fix it."

    print("\n--- SCRIPT TO READ ---\n")
    print(script.strip())
    print("\n--- END SCRIPT ---\n")

    # TTS
    ep_url = ""
    filesize = 0
    try:
        audio = tts_elevenlabs(script)
        if audio:
            ep_name = f"boston-briefing-{date_str}.mp3"
            ep_path = EP_DIR / ep_name
            ep_path.write_bytes(audio)
            filesize = len(audio)
            if PUBLIC_BASE_URL:
                ep_url = f"{PUBLIC_BASE_URL}/episodes/{ep_name}"
            print(f"[diag] saved MP3: {ep_path} ({filesize} bytes)")
        else:
            print("[diag] no audio produced (missing key/voice or TTS error)")
    except Exception as e:
        print(f"[warn] TTS step failed: {e}", file=sys.stderr)

    build_feed(ep_url, filesize)
    print("[diag] done.")

if __name__ == "__main__":
    main()
