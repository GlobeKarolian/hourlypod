import os, json, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------- Secrets / Config ----------
ELEVEN_API_KEY  = os.getenv("ELEVEN_API_KEY", "").strip()
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "").strip()
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o").strip()  # override in repo secrets if desired
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()     # e.g. https://<user>.github.io/<repo>

PUBLIC_DIR = Path("public")
EP_DIR     = PUBLIC_DIR / "episodes"
for d in (PUBLIC_DIR, EP_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---------- Helpers ----------
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

# ---------- OpenAI (GPT only; prompt comes from prompt.txt) ----------
def write_script_with_openai(prompt_text: str) -> str | None:
    if not OPENAI_API_KEY or not OPENAI_MODEL:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"[warn] openai import/init failed: {e}")
        return None

    _, tod, pretty_date = boston_now()
    sys_preamble = (
        "HARD CONSTRAINTS:\n"
        f"- Opening line MUST start exactly with: 'Good {tod}, it’s {pretty_date}.'\n"
        "- Lead with the day’s most important local news; do not lead with sports unless it is clearly the top story.\n"
        "- No editorializing or sympathy phrases. Neutral, public-radio tone. Natural attributions (e.g., 'The Globe reports…').\n"
        "- 5–8 crisp items with smooth transitions; end with quick weather + notable events; end disclosure line.\n"
    )

    # Prefer new Responses API for 'gpt-5*' models; fall back to Chat Completions otherwise
    try:
        if OPENAI_MODEL.lower().startswith("gpt-5"):
            resp = client.responses.create(
                model=OPENAI_MODEL,
                input=sys_preamble + "\n\n" + prompt_text.strip(),
                temperature=0.35,
                max_output_tokens=1400,
            )
            text = getattr(resp, "output_text", None) or ""
        else:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role":"system","content":sys_preamble},
                    {"role":"user","content":prompt_text.strip()},
                ],
                temperature=0.35,
                max_tokens=1400,
            )
            text = resp.choices[0].message.content or ""
        return text.strip() or None
    except Exception as e:
        print(f"[warn] OpenAI error: {e}")
        return None

# ---------- ElevenLabs TTS (optional) ----------
def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    payload = {
        "text": text,
        "voice_settings": {
            "stability": 0.55,
            "similarity_boost": 0.85,
            "style": 0.40,
            "use_speaker_boost": True
        },
        "voice_speed": 1.05,
        "model_id": "eleven_multilingual_v2"
    }
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=120)
    r.raise_for_status()
    return r.content

# ---------- Site/Feed helpers ----------
def write_index():
    # Static index for initial publish; the player page is index.html already present in /public.
    pass

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

def update_manifest(date_slug: str, title: str, url: str):
    manifest_path = PUBLIC_DIR / "manifest.json"
    data = []
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            data = []
    # Prepend newest
    data = [{"date": date_slug, "title": title, "url": url}] + [e for e in data if e.get("url") != url]
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

# ---------- Main ----------
def main():
    # 1) Load prompt
    prompt_text = Path("prompt.txt").read_text(encoding="utf-8") if Path("prompt.txt").exists() else ""

    # 2) Ask GPT to write the script
    script = None
    if prompt_text.strip():
        script = write_script_with_openai(prompt_text)

    # 3) Fallback message if GPT failed/empty
    if not script or len(script.split()) < 20:
        script = ("Ooops, something went wrong. Sorry about that. "
                  "Why don't you email Matt Karolian so I can fix it.")

    # Log script in CI
    print("\\n--- SCRIPT TO READ ---\\n")
    print(script.strip())
    print("\\n--- END SCRIPT ---\\n")

    # 4) Synthesize with ElevenLabs (optional)
    ep_bytes = None
    try:
        ep_bytes = tts_elevenlabs(script)
    except Exception as ex:
        print(f"[warn] ElevenLabs error: {ex}")

    # 5) Save episode + update feed + manifest
    now, _, _ = boston_now()
    date_str = now.strftime("%Y-%m-%d")
    ep_name = f"boston-briefing-{date_str}.mp3"
    ep_url = ""
    size = 0
    if ep_bytes:
        (EP_DIR / ep_name).write_bytes(ep_bytes)
        size = len(ep_bytes)
        if PUBLIC_BASE_URL:
            ep_url = f"{PUBLIC_BASE_URL}/episodes/{ep_name}"

    build_feed(ep_url, size)
    update_manifest(date_str, f"Boston Briefing – {date_str}", ep_url or ep_name)

if __name__ == "__main__":
    main()
