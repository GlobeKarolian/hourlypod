import os
import sys
import json
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
from openai import OpenAI
import requests
import elevenlabs_config as el_config

PUBLIC_DIR = Path("public")
EPISODES_DIR = PUBLIC_DIR / "episodes"

for d in (PUBLIC_DIR, EPISODES_DIR):
    d.mkdir(parents=True, exist_ok=True)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5").strip()
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY", "").strip()
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "").strip()

PROMPT_FILE = Path("prompt.txt")

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

def get_prompt():
    if PROMPT_FILE.exists():
        return PROMPT_FILE.read_text(encoding="utf-8").strip()
    return ""

def rewrite_with_gpt(prompt_text: str) -> str | None:
    if not OPENAI_API_KEY or not OPENAI_MODEL:
        return None
    client = OpenAI(api_key=OPENAI_API_KEY)
    now, tod, pretty_date = boston_now()
    control = (
        f"Time-of-day greeting must be: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Pull the top real news stories from Boston.com, Boston Globe, and B-Side.\n"
        "- No editorializing, only factual reporting.\n"
        "- Lead with the most important news, save sports for later unless it's the top story.\n"
        "- Smooth transitions, concise but natural, like a professional public radio host.\n"
        "- End with quick weather + notable events, then disclose this is AI-written & voiced.\n"
    )
    try:
        if OPENAI_MODEL.lower().startswith("gpt-5"):
            resp = client.responses.create(
                model=OPENAI_MODEL,
                input=f"{control}\n\n{prompt_text}",
                max_output_tokens=1200
            )
            return (getattr(resp, "output_text", None) or "").strip()
        else:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": f"{control}\n\n{prompt_text}"}],
                temperature=0.35,
                max_tokens=1200
            )
            return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[error] OpenAI error: {repr(e)}", file=sys.stderr)
        return None

def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    payload = {
        "text": text,
        "voice_settings": {
            "stability": el_config.STABILITY,
            "similarity_boost": el_config.SIMILARITY_BOOST,
            "style": el_config.STYLE,
            "use_speaker_boost": el_config.USE_SPEAKER_BOOST
        },
        "voice_speed": el_config.VOICE_SPEED,
        "model_id": el_config.MODEL_ID
    }
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=120)
    r.raise_for_status()
    return r.content

def write_index():
    url = f"{PUBLIC_BASE_URL}/feed.xml" if PUBLIC_BASE_URL else "feed.xml"
    html = f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'/>
<title>Boston Briefing – Internal Beta</title>
</head>
<body>
<div style="background:yellow;padding:10px;font-weight:bold;">INTERNAL BETA / TEST — Do not share externally.</div>
<h1>Boston Briefing</h1>
<p><a href="{url}">Podcast RSS Feed</a></p>
<p><a href="shownotes/">Show Notes</a></p>
<audio controls style="width:100%;margin-top:20px;">
<source src="episodes/latest.mp3" type="audio/mpeg">
Your browser does not support the audio element.
</audio>
</body>
</html>"""
    (PUBLIC_DIR / "index.html").write_text(html, encoding="utf-8")

def build_feed(episode_url: str, filesize: int):
    now = dt.datetime.now()
    item_title = f"Boston Briefing – {now.strftime('%Y-%m-%d')}"
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Boston Briefing</title>
    <link>{PUBLIC_BASE_URL}</link>
    <language>en-us</language>
    <description>Boston's daily AI-generated news briefing.</description>
    <itunes:author>Boston Briefing</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <lastBuildDate>{now.strftime('%a, %d %b %Y %H:%M:%S %z')}</lastBuildDate>
    <item>
      <title>{item_title}</title>
      <description>Boston's daily AI-generated news briefing.</description>
      <link>{episode_url}</link>
      <guid isPermaLink="false">{episode_url}</guid>
      <pubDate>{now.strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate>
      <enclosure url="{episode_url}" length="{filesize}" type="audio/mpeg"/>
    </item>
  </channel>
</rss>"""
    (PUBLIC_DIR / "feed.xml").write_text(feed, encoding="utf-8")

def main():
    prompt_text = get_prompt()
    script = rewrite_with_gpt(prompt_text)
    if not script or len(script.split()) < 20:
        script = "Ooops, something went wrong. Sorry about that. Why don't you email Matt Karolian so I can fix it."

    print("\n--- SCRIPT TO READ ---\n")
    print(script)
    print("\n--- END SCRIPT ---\n")

    today = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = today.strftime("%Y-%m-%d")
    write_index()

    mp3_bytes = tts_elevenlabs(script)
    ep_url = ""
    filesize = 0
    if mp3_bytes:
        ep_name = f"boston-briefing-{date_str}.mp3"
        ep_path = EPISODES_DIR / ep_name
        ep_path.write_bytes(mp3_bytes)
        filesize = len(mp3_bytes)
        if PUBLIC_BASE_URL:
            ep_url = f"{PUBLIC_BASE_URL}/episodes/{ep_name}"

    build_feed(ep_url, filesize)

if __name__ == "__main__":
    main()
