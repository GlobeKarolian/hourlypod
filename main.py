# main.py
import os, sys, io, re, json, datetime as dt
from pathlib import Path
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import yaml, feedparser, requests
from bs4 import BeautifulSoup
from readability import Document
import trafilatura
from rapidfuzz import fuzz, process
from pydub import AudioSegment

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
            resp = _client.responses.create(
                model=OPENAI_MODEL,
                input=f"{sys_preamble}\n\n{prompt_text.strip()}\n\n{user_block}",
                max_output_tokens=1200,
            )
            return (getattr(resp, "output_text", "") or "").strip()
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
        print(f"[warn] OpenAI generation failed: {e}")
        return None

# -------------------- SANITIZER --------------------
def sanitize_for_tts(s: str) -> str:
    for a,b in (("—", ", "), ("–", ", "), ("…", ". "), (" / ", " or ")):
        s = s.replace(a,b)
    s = s.replace("MBTA", "M-B-T-A").replace("BPL", "B-P-L")
    return " ".join(s.split())

# -------------------- CHUNKED ELEVENLABS TTS --------------------
# sentence splitter and chunker (~2–3 sentences, <= 320 chars per chunk)
_SENT_END = re.compile(r'(?<=[\.\?\!])\s+(?=[A-Z0-9“"])')
def split_into_chunks(text: str, max_len: int = 320):
    sents = [s.strip() for s in _SENT_END.split(text.strip()) if s.strip()]
    chunks, cur = [], ""
    for s in sents:
        if not cur: cur = s
        elif len(cur) + 1 + len(s) <= max_len: cur = f"{cur} {s}"
        else: chunks.append(cur); cur = s
    if cur: chunks.append(cur)
    return chunks

def _eleven_call_single(text: str) -> bytes | None:
    # Clean non-streaming endpoint; ONE request per chunk; top-level voice_speed
    base = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    url  = f"{base}?output_format=mp3_44100_128"
    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",   #
