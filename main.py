# main.py
import os, sys, json, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml, feedparser, requests, trafilatura
from bs4 import BeautifulSoup
from readability import Document
from rapidfuzz import fuzz, process

# ---------- Secrets / Config ----------
ELEVEN_API_KEY   = os.getenv("ELEVEN_API_KEY", "").strip()
ELEVEN_VOICE_ID  = os.getenv("ELEVEN_VOICE_ID", "").strip()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o").strip()
PUBLIC_BASE_URL  = os.getenv("PUBLIC_BASE_URL", "").strip()
NEWSAPI_KEY      = os.getenv("NEWSAPI_KEY", "").strip()
MAX_ITEMS        = int(os.getenv("MAX_ITEMS", "10"))

PUBLIC_DIR = Path("public")
EP_DIR     = PUBLIC_DIR / "episodes"
SH_NOTES   = PUBLIC_DIR / "shownotes"
for d in (PUBLIC_DIR, EP_DIR, SH_NOTES):
    d.mkdir(parents=True, exist_ok=True)

# ---------- Load config files ----------
with open("feeds.yml", "r", encoding="utf-8") as f:
    feeds_cfg = yaml.safe_load(f) or {}
SOURCES     = feeds_cfg.get("sources", [])
EXCLUDE     = set(str(k).lower() for k in feeds_cfg.get("exclude_keywords", []))
LIMIT_PER   = int(feeds_cfg.get("daily_limit_per_source", feeds_cfg.get("daily_limit_per_source", 6)))

VOICE_SETTINGS_PATH = Path("voice_settings.json")
DEFAULT_VOICE_SETTINGS = {
    "stability": 0.58,
    "similarity_boost": 0.88,
    "style": 0.35,
    "use_speaker_boost": True,
    "voice_speed": 1.03,     # a touch brisker for news; adjust in voice_settings.json
    "model_id": "eleven_multilingual_v2"
}
if VOICE_SETTINGS_PATH.exists():
    try:
        DEFAULT_VOICE_SETTINGS.update(json.loads(VOICE_SETTINGS_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass

# ---------- Helpers ----------
def is_newsworthy(title: str) -> bool:
    t = (title or "").lower()
    return t and not any(k in t for k in EXCLUDE)

def fetch_items_from_rss():
    out = []
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
                out.append({"source": name, "title": title, "link": link})
                count += 1
        except Exception as ex:
            print(f"[warn] feed error {name}: {ex}", file=sys.stderr)
    return out

def fetch_from_newsapi(cfg) -> list[dict]:
    """Optional: query NewsAPI to broaden coverage (Globe, Boston.com, B-Side, AP/Reuters Boston hits, etc.)."""
    if not NEWSAPI_KEY:
        return []
    news_cfg = (cfg or {}).get("newsapi", {})
    # If user sets newsapi.enabled: false, skip; else default to enabled when key exists
    if news_cfg.get("enabled") is False:
        return []

    domains  = news_cfg.get("domains", [])  # restrict to certain domains if you want
    queries  = news_cfg.get("queries", ["Boston", "Massachusetts", "Boston City Council", "MBTA", "Mayor Wu"])
    page_sz  = int(news_cfg.get("page_size", 50))

    base = "https://newsapi.org/v2/everything"
    headers = {"X-Api-Key": NEWSAPI_KEY}
    q = " OR ".join([q for q in queries if q.strip()]) or "Boston"

    params = {
        "q": q,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_sz,
    }
    if domains:
        params["domains"] = ",".join(domains)

    items = []
    try:
        r = requests.get(base, headers=headers, params=params, timeout=25)
        r.raise_for_status()
        data = r.json()
        for a in (data.get("articles") or []):
            title = (a.get("title") or "").strip()
            url   = (a.get("url") or "").strip()
            src   = (a.get("source", {}).get("name") or "NewsAPI").strip()
            if title and url and is_newsworthy(title):
                items.append({"source": src, "title": title, "link": url})
    except Exception as ex:
        print(f"[warn] newsapi error: {ex}", file=sys.stderr)
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
    # 1) trafilatura
    try:
        downloaded = trafilatura.fetch_url(url, timeout=20)
        if downloaded:
            extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if extracted and len(extracted.split()) > 40:
                return extracted
    except Exception:
        pass
    # 2) readability
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
    notes, used = [], 0
    for it in items:
        if used >= MAX_ITEMS: break
        txt = extract_text(it["link"])
        if not txt: continue
        sent = first_sentence(txt)
        if len(sent.split()) < 6: continue
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

def _responses_api(prompt_text: str, notes: list[str], model: str) -> str:
    # Avoid params some models don’t accept (e.g., temperature). Keep it minimal.
    now, tod, pretty_date = boston_now()
    control = (
        "HARD CONSTRAINTS (do not violate):\n"
        f"- Opening must be: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Lead with the most important news; do NOT lead with sports unless it’s clearly the top story.\n"
        "- No editorializing, sympathy, or sentiment. Just factual, concise, and human-sounding.\n"
        "- Integrate source names naturally (e.g., 'The Globe reports…', 'Boston.com says…', 'B-Side notes…').\n"
        "- 5–8 items; smooth transitions; then quick weather + 1–2 notable events; end with the beta disclosure.\n"
    )
    user_block = "STORIES (verbatim notes):\n" + "\n\n".join(notes)
    full_input = f"{control}\n\nSTYLE PROMPT:\n{prompt_text.strip()}\n\n{user_block}"

    # Some deployments expect 'input' only + max_output_tokens
    resp = _client.responses.create(
        model=model,
        input=full_input,
        max_output_tokens=1200,   # keep within cost; raise if you like
    )
    return (getattr(resp, "output_text", None) or "").strip()

def _chat_api(prompt_text: str, notes: list[str], model: str) -> str:
    now, tod, pretty_date = boston_now()
    control = (
        "HARD CONSTRAINTS (do not violate):\n"
        f"- Opening must be: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Lead with the most important news; do NOT lead with sports unless it’s clearly the top story.\n"
        "- No editorializing, sympathy, or sentiment. Just factual, concise, and human-sounding.\n"
        "- Integrate source names naturally (e.g., 'The Globe reports…', 'Boston.com says…', 'B-Side notes…').\n"
        "- 5–8 items; smooth transitions; then quick weather + 1–2 notable events; end with the beta disclosure.\n"
    )
    user_block = "STORIES (verbatim notes):\n" + "\n\n".join(notes)
    resp = _client.chat.completions.create(
        model=model,
        messages=[
            {"role":"system","content":control},
            {"role":"user","content":f"{prompt_text.strip()}\n\n{user_block}"},
        ],
        temperature=0.35,
        max_tokens=1200,
    )
    return resp.choices[0].message.content.strip()

def rewrite_with_openai(prompt_text: str, notes: list[str]) -> str | None:
    if not _client or not OPENAI_MODEL:
        return None
    try:
        # Prefer Responses API for GPT-5* names; fall back to Chat
        if OPENAI_MODEL.lower().startswith("gpt-5"):
            try:
                return _responses_api(prompt_text, notes, OPENAI_MODEL)
            except Exception as e:
                print(f"[warn] Responses API failed: {e}", file=sys.stderr)
                return _chat_api(prompt_text, notes, "gpt-4o")
        else:
            return _chat_api(prompt_text, notes, OPENAI_MODEL)
    except Exception as e:
        print(f"[warn] OpenAI error: {e}", file=sys.stderr)
        try:
            return _chat_api(prompt_text, notes, "gpt-4o")
        except Exception as e2:
            print(f"[warn] OpenAI fallback failed: {e2}", file=sys.stderr)
            return None

# ---------- ElevenLabs ----------
def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"

    vs = DEFAULT_VOICE_SETTINGS.copy()
    payload = {
        "text": text,
        "voice_settings": {
            "stability": vs.get("stability", 0.58),
            "similarity_boost": vs.get("similarity_boost", 0.88),
            "style": vs.get("style", 0.35),
            "use_speaker_boost": bool(vs.get("use_speaker_boost", True))
        },
        "voice_speed": vs.get("voice_speed", 1.03),
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

# ---------- Site output ----------
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

# ---------- Main ----------
def main():
    # 1) Collect candidates
    items_rss = fetch_items_from_rss()
    items_api = fetch_from_newsapi(feeds_cfg)  # optional; on if NEWSAPI_KEY and not disabled
    items = dedupe(items_rss + items_api)

    # 2) Extract short notes
    notes = build_notes(items)

    # 3) Load newsroom style prompt
    prompt_text = Path("prompt.txt").read_text(encoding="utf-8") if Path("prompt.txt").exists() else ""

    # 4) GPT rewrite
    gpt_script = None
    if prompt_text and notes:
        gpt_script = rewrite_with_openai(prompt_text, notes)

    # 5) Fallback if GPT fails/empty
    if gpt_script and len(gpt_script.split()) > 20:
        final_script = gpt_script
    else:
        final_script = (
            "Ooops, something went wrong. Sorry about that. "
            "Why don't you email Matt Karolian so I can fix it."
        )

    # Log script for inspection
    print("\n--- SCRIPT TO READ ---\n")
    print(final_script.strip())
    print("\n--- END SCRIPT ---\n")

    # 6) Site artifacts
    today = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = today.strftime("%Y-%m-%d")
    write_shownotes(date_str, items)
    write_index()

    # 7) TTS + feed
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
