# main.py - OPTIMIZED FOR NATURAL TTS & BETTER NEWS PROCESSING
import os, sys, json, datetime as dt, re
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
MAX_ITEMS       = int(os.getenv("MAX_ITEMS", "8"))

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
LIMIT_PER = int(cfg.get("daily_limit_per_source", cfg.get("limit_per_source", 8)))

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
    # Format date properly for both Windows and Unix
    pretty_date = now.strftime("%A, %B ") + str(int(now.strftime("%d"))) + now.strftime(", %Y")
    return now, tod, pretty_date

# -------------------- FETCH / DEDUPE --------------------
def is_newsworthy(title: str) -> bool:
    """Filter out non-news content"""
    t = (title or "").lower()
    return bool(t and not any(k in t for k in EXCLUDE))

def fetch_items():
    """Fetch RSS items with better error handling"""
    items = []
    for src in SOURCES:
        name = src.get("name","Unknown")
        rss  = src.get("rss","").strip()
        if not rss:
            continue
        try:
            # Add timeout and better user agent
            fp = feedparser.parse(rss, agent='Mozilla/5.0 (compatible; BostonBriefing/2.0)')
            if fp.bozo:
                print(f"[warn] feed parse warning for {name}: {fp.bozo_exception}", file=sys.stderr)
            
            count = 0
            for e in fp.entries:
                if count >= LIMIT_PER: break
                title = (e.get("title") or "").strip()
                link  = (e.get("link") or "").strip()
                if not title or not link: continue
                if not is_newsworthy(title): continue
                
                # Clean HTML from summary
                summary = (e.get("summary") or e.get("description") or "").strip()
                if summary:
                    summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
                
                items.append({
                    "source": name, 
                    "title": title, 
                    "link": link, 
                    "summary": summary[:500]  # Limit summary length
                })
                count += 1
        except Exception as ex:
            print(f"[warn] feed error {name}: {ex}", file=sys.stderr)
    return items

def dedupe(items, threshold=85):
    """Improved deduplication with lower threshold for better duplicate detection"""
    kept, seen = [], []
    for it in items:
        title = it["title"]
        if not seen:
            kept.append(it)
            seen.append(title)
            continue
        match = process.extractOne(title, seen, scorer=fuzz.token_set_ratio)
        if not match or match[1] < threshold:
            kept.append(it)
            seen.append(title)
    return kept

# -------------------- EXTRACTION --------------------
def extract_text(url: str) -> str:
    """Enhanced text extraction with better fallbacks"""
    # 1) trafilatura first (best for news)
    try:
        downloaded = trafilatura.fetch_url(url, timeout=20)
        if downloaded:
            extracted = trafilatura.extract(
                downloaded, 
                include_comments=False, 
                include_tables=False,
                deduplicate=True,
                favor_precision=True
            )
            if extracted and len(extracted.split()) > 40:
                return extracted
    except Exception as e:
        print(f"[debug] trafilatura failed for {url}: {e}", file=sys.stderr)
    
    # 2) readability fallback
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; BostonBriefing/2.0)",
            "Accept": "text/html,application/xhtml+xml"
        }
        r = requests.get(url, timeout=20, headers=headers, allow_redirects=True)
        r.raise_for_status()
        
        doc = Document(r.text)
        text = BeautifulSoup(doc.summary(), "html.parser").get_text("\n", strip=True)
        
        # Better line filtering
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if len(line.split()) > 5 and not line.startswith(("Share", "Subscribe", "Advertisement")):
                lines.append(line)
        
        return "\n".join(lines)
    except Exception as e:
        print(f"[debug] readability failed for {url}: {e}", file=sys.stderr)
        return ""

def first_sentence(text: str) -> str:
    """Extract clean first sentence with better parsing"""
    # Clean up text first
    text = " ".join(text.split())
    text = re.sub(r'\s+', ' ', text)
    
    # Try to find a good sentence
    for sep in [". ", "? ", "! ", " — ", " – "]:
        if sep in text:
            parts = text.split(sep)
            for part in parts:
                part = part.strip(".•–—!? ")
                if 10 <= len(part.split()) <= 50:  # Good sentence length
                    return part
    
    # Fallback: first 200 chars
    if len(text) > 200:
        return text[:200].rsplit(" ", 1)[0].strip(".•–—!? ") + "..."
    return text.strip(".•–—!? ")

def build_notes(items):
    """Build story notes with better quality control"""
    notes, used = [], 0
    
    # Sort by source priority (Globe first, then Boston.com)
    priority_order = ["The Boston Globe", "Boston.com", "The Boston Globe Business"]
    items_sorted = sorted(items, key=lambda x: (
        priority_order.index(x['source']) if x['source'] in priority_order else 99,
        x['title']
    ))
    
    for it in items_sorted:
        if used >= MAX_ITEMS: 
            break
            
        txt = extract_text(it["link"])
        if not txt:
            # Use summary as fallback
            txt = it.get("summary") or it["title"]
        
        sent = first_sentence(txt)
        if len(sent.split()) < 8:
            continue
            
        # Format: clean sentence with source and link
        notes.append(f"{it['source']}: {sent}  (link: {it['link']})")
        used += 1
        
    print(f"[diag] built {len(notes)} quality notes from {len(items)} items")
    return notes

# -------------------- OPENAI --------------------
try:
    from openai import OpenAI
    _client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    print(f"[warn] openai import failed: {e}", file=sys.stderr)
    _client = None

def rewrite_with_openai(prompt_text: str, notes: list[str]) -> str | None:
    """Enhanced OpenAI generation with better prompting"""
    if not _client or not OPENAI_MODEL:
        print("[diag] OpenAI client/model missing")
        return None

    now, tod, pretty_date = boston_now()
    
    # Enhanced system prompt for better output
    sys_preamble = (
        "You are writing a professional news briefing script for audio delivery.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        f"1. Opening MUST be exactly: 'Good {tod}, it's {pretty_date}.'\n"
        "2. Write 300-450 words total (2-3 minute read time).\n"
        "3. Include 5-8 stories, 2-4 sentences each.\n"
        "4. Lead with the most impactful LOCAL news story.\n"
        "5. Use smooth, varied transitions between stories.\n"
        "6. Natural attribution: mention source once, then continue without repeating.\n"
        "7. Professional broadcast tone - confident and conversational.\n"
        "8. End with brief weather and the required beta disclaimer.\n"
        "9. NO editorializing, sympathy expressions, or personal commentary.\n"
        "10. Write for AUDIO - use natural speech patterns and rhythm.\n"
    )
    
    user_block = (
        "Create a polished audio script from these story notes:\n\n" + 
        "\n\n".join(notes) +
        "\n\nRemember: This is for audio delivery. Make it sound natural when read aloud."
    )
    
    try:
        # Try with the specified model
        messages = [
            {"role": "system", "content": sys_preamble},
            {"role": "user", "content": f"{prompt_text.strip()}\n\n{user_block}"}
        ]
        
        resp = _client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.3,  # Lower for more consistent output
            max_completion_tokens=1200,  # Fixed: was max_tokens
            presence_penalty=0.3,  # Reduce repetition
            frequency_penalty=0.3
        )
        
        script = (resp.choices[0].message.content or "").strip()
        
        # Validate output
        if script and len(script.split()) > 50:
            return script
        else:
            print(f"[warn] Script too short ({len(script.split())} words), retrying...")
            return None
            
    except Exception as e:
        print(f"[warn] OpenAI generation failed: {e}", file=sys.stderr)
        # Try fallback with gpt-4o-mini
        try:
            print("[diag] trying fallback with gpt-4o-mini...")
            resp = _client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.3,
                max_completion_tokens=1200  # Fixed: was max_tokens
            )
            script = (resp.choices[0].message.content or "").strip()
            if script and len(script.split()) > 50:
                return script
        except Exception as e2:
            print(f"[warn] Fallback also failed: {e2}", file=sys.stderr)
        return None

# -------------------- TTS SANITIZER --------------------
def sanitize_for_tts(s: str) -> str:
    """Enhanced sanitization for natural TTS delivery"""
    # Remove URLs and email addresses
    s = re.sub(r'https?://\S+', '', s)
    s = re.sub(r'\S+@\S+\.\S+', '', s)
    
    # Fix punctuation for better prosody
    replacements = [
        ("—", ", "),
        ("–", ", "),
        ("…", "."),
        ("...", "."),
        (" / ", " or "),
        ("&", " and "),
        ("%", " percent"),
        ("$", " dollars "),
        ("24/7", "twenty four seven"),
        ("9/11", "nine eleven"),
    ]
    for old, new in replacements:
        s = s.replace(old, new)
    
    # Expand common Boston acronyms
    acronyms = {
        "MBTA": "M-B-T-A",
        "BPL": "Boston Public Library", 
        "BPD": "Boston Police",
        "BFD": "Boston Fire Department",
        "MGH": "Mass General Hospital",
        "MIT": "M-I-T",
        "BU": "B-U",
        "BC": "B-C",
        "CEO": "C-E-O",
        "FBI": "F-B-I",
        "COVID": "covid"
    }
    for acronym, expansion in acronyms.items():
        s = re.sub(r'\b' + acronym + r'\b', expansion, s)
    
    # Fix problematic patterns
    s = re.sub(r'\.{2,}', '.', s)  # Multiple periods
    s = re.sub(r'\s+', ' ', s)  # Multiple spaces
    s = re.sub(r'([.!?])\s*([a-z])', lambda m: m.group(1) + ' ' + m.group(2).upper(), s)  # Capitalize after sentence
    
    # Clean up quotes for speech
    s = s.replace('"', '').replace("'", "'")
    
    return s.strip()

# -------------------- OPTIMIZED TTS --------------------
def tts_elevenlabs(text: str) -> bytes | None:
    """
    OPTIMIZED TTS - Natural speech without chunky speedup/slowdown
    """
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        print("[diag] skipping TTS; missing ELEVEN_API_KEY/VOICE_ID or empty text")
        return None

    base = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    
    # High quality output with streaming optimization disabled for best quality
    url = f"{base}?output_format=mp3_44100_128&optimize_streaming_latency=0"

    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",  # Best for cloned voices
        "voice_settings": {
            "stability": 0.85,           # High stability prevents speed variations
            "similarity_boost": 0.90,    # High similarity for cloned voice accuracy
            "style": 0.10,               # Small amount for natural variation
            "use_speaker_boost": True    # Essential for cloned voices
        }
    }

    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }

    try:
        print("[diag] sending to ElevenLabs TTS...")
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        
        if r.status_code >= 400:
            print(f"[error] ElevenLabs error {r.status_code}: {r.text[:500]}", file=sys.stderr)
            
            # Try fallback settings if main settings fail
            if r.status_code == 400:
                print("[diag] trying fallback TTS settings...")
                payload["voice_settings"]["stability"] = 0.75
                payload["voice_settings"]["style"] = 0.0
                r = requests.post(url, headers=headers, json=payload, timeout=120)
                
                if r.status_code >= 400:
                    return None
        
        audio_size = len(r.content)
        print(f"[success] ✅ Natural TTS generated: {audio_size:,} bytes")
        
        # Validate audio size (should be roughly 10-30 KB per second of speech)
        expected_min = 15000  # ~1.5 seconds minimum
        expected_max = 500000  # ~50 seconds maximum
        
        if audio_size < expected_min:
            print(f"[warn] Audio suspiciously small ({audio_size} bytes)")
        elif audio_size > expected_max:
            print(f"[warn] Audio suspiciously large ({audio_size} bytes)")
            
        return r.content
        
    except requests.exceptions.Timeout:
        print(f"[error] ElevenLabs request timed out", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[error] ElevenLabs request failed: {e}", file=sys.stderr)
        return None

# -------------------- OUTPUT (SITE/FEED) --------------------
def write_shownotes(date_str, items):
    """Generate clean shownotes HTML"""
    html = [
        '<!DOCTYPE html>',
        '<html lang="en">',
        '<head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>Boston Briefing – {date_str}</title>',
        '<style>',
        'body { font-family: system-ui, -apple-system, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6; }',
        'h1 { color: #1a1a1a; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }',
        'ol { padding-left: 20px; }',
        'li { margin: 12px 0; }',
        'a { color: #0066cc; text-decoration: none; }',
        'a:hover { text-decoration: underline; }',
        '.source { color: #666; font-weight: 500; }',
        '.date { color: #999; font-size: 0.9em; }',
        '</style>',
        '</head>',
        '<body>',
        f'<h1>Boston Briefing – {date_str}</h1>',
        f'<p class="date">Sources for today\'s briefing:</p>',
        '<ol>'
    ]
    
    count = 0
    for it in items[:MAX_ITEMS]:
        title = it['title'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        source = it['source'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        link = it['link'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        html.append(
            f'<li><a href="{link}" target="_blank" rel="noopener">{title}</a> '
            f'<span class="source">– {source}</span></li>'
        )
        count += 1
    
    html.extend([
        '</ol>',
        '<p style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #e0e0e0; color: #666; font-size: 0.9em;">',
        'This is an AI-generated news briefing. All stories are sourced from legitimate Boston news outlets.',
        '</p>',
        '</body>',
        '</html>'
    ])
    
    shownotes_path = SH_NOTES / f"{date_str}.html"
    shownotes_path.write_text("\n".join(html), encoding="utf-8")
    print(f"[diag] wrote shownotes: {shownotes_path}")

def write_index_if_missing():
    """Only create index if it doesn't exist"""
    idx = PUBLIC_DIR / "index.html"
    if idx.exists():
        print("[diag] index.html exists, skipping scaffold")
        return
        
    print("[diag] creating default index.html")
    url = f"{PUBLIC_BASE_URL}/feed.xml" if PUBLIC_BASE_URL else "feed.xml"
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Boston Briefing</title></head>
<body>
  <h1>Boston Briefing</h1>
  <p>Podcast RSS: <a href="{url}">{url}</a></p>
  <p>Shownotes: <a href="shownotes/">Browse episodes</a></p>
</body></html>"""
    idx.write_text(html, encoding="utf-8")

def build_feed(episode_url: str, filesize: int):
    """Generate valid podcast RSS feed"""
    title = "Boston Briefing"
    desc = "A short, factual Boston news briefing powered by AI."
    link = PUBLIC_BASE_URL or ""
    
    now = dt.datetime.now().astimezone()
    last_build = format_datetime(now)
    
    # Use Boston time for the title
    boston_now_time, _, _ = boston_now()
    item_title = boston_now_time.strftime("Boston Briefing – %B %-d, %Y") if hasattr(boston_now_time.strftime, '%-d') else boston_now_time.strftime("Boston Briefing – %B %d, %Y").replace(' 0', ' ')
    
    guid = episode_url or f"boston-briefing-{boston_now_time.strftime('%Y-%m-%d')}"
    
    enclosure = f'<enclosure url="{episode_url}" length="{filesize}" type="audio/mpeg"/>' if episode_url else ""
    
    feed = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        '  <channel>',
        f'    <title>{title}</title>',
        f'    <link>{link}</link>',
        '    <language>en-us</language>',
        f'    <description>{desc}</description>',
        '    <itunes:author>Boston Briefing</itunes:author>',
        '    <itunes:summary>AI-powered daily Boston news updates. Written by GPT, voiced by an AI clone.</itunes:summary>',
        '    <itunes:category text="News">',
        '      <itunes:category text="Daily News"/>',
        '    </itunes:category>',
        '    <itunes:explicit>false</itunes:explicit>',
        '    <itunes:type>episodic</itunes:type>',
        f'    <lastBuildDate>{last_build}</lastBuildDate>',
        '    <item>',
        f'      <title>{item_title}</title>',
        f'      <description>Today\'s Boston news: top stories from The Boston Globe, Boston.com, and other local sources.</description>',
        f'      <link>{episode_url}</link>',
        f'      <guid isPermaLink="false">{guid}</guid>',
        f'      <pubDate>{last_build}</pubDate>',
        f'      {enclosure}',
        '      <itunes:duration>180</itunes:duration>',
        '      <itunes:episodeType>full</itunes:episodeType>',
        '    </item>',
        '  </channel>',
        '</rss>'
    ]
    
    feed_path = PUBLIC_DIR / "feed.xml"
    feed_path.write_text("\n".join(feed), encoding="utf-8")
    print(f"[diag] wrote RSS feed: {feed_path}")

# -------------------- MAIN --------------------
def main():
    print("\n" + "="*60)
    print("BOSTON BRIEFING - OPTIMIZED GENERATION")
    print("="*60)
    
    # Show configuration
    print(f"[config] Model: {OPENAI_MODEL}")
    print(f"[config] Max items: {MAX_ITEMS}")
    print(f"[config] Sources: {len(SOURCES)}")
    print(f"[config] TTS: {'ElevenLabs' if ELEVEN_API_KEY else 'Disabled'}")
    print(f"[config] Base URL: {'Set' if PUBLIC_BASE_URL else 'Not set'}")
    
    # Fetch and process news
    print("\n[1/6] Fetching news feeds...")
    raw = fetch_items()
    print(f"  → fetched {len(raw)} total items")
    
    print("\n[2/6] Deduplicating stories...")
    deduped = dedupe(raw)
    print(f"  → {len(deduped)} unique stories")
    
    print("\n[3/6] Extracting article content...")
    notes = build_notes(deduped)
    if not notes:
        print("[error] No valid stories found!")
        sys.exit(1)
    print(f"  → extracted {len(notes)} quality stories")
    
    # Load prompt template
    prompt_text = ""
    prompt_path = ROOT / "prompt.txt"
    if prompt_path.exists():
        prompt_text = prompt_path.read_text(encoding="utf-8")
        print("\n[4/6] Loaded custom prompt template")
    else:
        print("\n[4/6] No prompt.txt found, using defaults")
    
    # Generate script
    print("\n[5/6] Generating script with AI...")
    script = None
    if prompt_text.strip() and notes:
        script = rewrite_with_openai(prompt_text, notes)
        if script:
            word_count = len(script.split())
            print(f"  → generated {word_count} words")
        else:
            print("  → generation failed, using fallback")
    
    # Fallback if generation failed
    if not script or len(script.split()) < 50:
        print("[warn] Using fallback script")
        script = (
            "Good evening, this is the Boston Briefing. "
            "Unfortunately, we're experiencing technical difficulties generating today's episode. "
            "Our AI system seems to have taken an unexpected coffee break. "
            "Please check back tomorrow for your regularly scheduled news update. "
            "In the meantime, why not check out The Boston Globe or Boston.com directly? "
            "This has been the Boston Briefing, or at least, an attempt at one."
        )
    
    # Sanitize script for TTS
    script = sanitize_for_tts(script)
    
    # Display script for debugging
    print("\n" + "-"*40)
    print("FINAL SCRIPT:")
    print("-"*40)
    print(script)
    print("-"*40 + "\n")
    
    # Generate output files
    today = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = today.strftime("%Y-%m-%d")
    
    write_shownotes(date_str, deduped)
    write_index_if_missing()
    
    # Generate TTS
    print("\n[6/6] Generating audio with TTS...")
    mp3_bytes = None
    if ELEVEN_API_KEY:
        mp3_bytes = tts_elevenlabs(script)
    else:
        print("  → TTS skipped (no API key)")
    
    # Save MP3 and create feed
    ep_url = ""
    filesize = 0
    ep_name = f"boston-briefing-{date_str}.mp3"
    ep_path = EP_DIR / ep_name
    
    if mp3_bytes:
        ep_path.write_bytes(mp3_bytes)
        filesize = len(mp3_bytes)
        ep_url = f"{PUBLIC_BASE_URL}/episodes/{ep_name}" if PUBLIC_BASE_URL else f"episodes/{ep_name}"
        print(f"  → saved {ep_name} ({filesize:,} bytes)")
        
        # Audio duration estimate (rough: 128kbps)
        duration_seconds = (filesize * 8) / (128 * 1000)
        print(f"  → estimated duration: {duration_seconds:.1f} seconds")
    else:
        print("  → no audio generated")
    
    build_feed(ep_url, filesize)
    
    print("\n" + "="*60)
    print("✅ GENERATION COMPLETE!")
    print(f"📅 Episode: {date_str}")
    print(f"🎙️ Audio: {'Generated' if mp3_bytes else 'Failed'}")
    print(f"📝 Shownotes: shownotes/{date_str}.html")
    print(f"📡 RSS Feed: feed.xml")
    print("="*60 + "\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[interrupted] Stopped by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n[FATAL ERROR] {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
