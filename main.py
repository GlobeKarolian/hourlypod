# Add this function after your imports and before the TTS function

def load_voice_settings():
    """Load voice settings from external config files."""
    # Try voice_settings.json first
    voice_json_path = ROOT / "voice_settings.json"
    if voice_json_path.exists():
        try:
            with open(voice_json_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
                print(f"[diag] Loaded voice settings from {voice_json_path}")
                return settings
        except Exception as e:
            print(f"[warn] Failed to load {voice_json_path}: {e}")
    
    # Fallback to elevenlabs_config.py
    config_path = ROOT / "elevenlabs_config.py"
    if config_path.exists():
        try:
            # Import the config values
            import sys
            sys.path.insert(0, str(ROOT))
            import elevenlabs_config as cfg
            settings = {
                "model_id": cfg.MODEL_ID,
                "voice_settings": {
                    "stability": cfg.STABILITY,
                    "similarity_boost": cfg.SIMILARITY_BOOST,
                    "style": cfg.STYLE,
                    "use_speaker_boost": cfg.USE_SPEAKER_BOOST
                },
                "voice_speed": cfg.VOICE_SPEED
            }
            print(f"[diag] Loaded voice settings from {config_path}")
            return settings
        except Exception as e:
            print(f"[warn] Failed to load {config_path}: {e}")
    
    # Final fallback to hardcoded values
    print("[diag] Using fallback voice settings")
    return {
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.85,
            "similarity_boost": 0.90,
            "style": 0.15,
            "use_speaker_boost": True
        },
        "voice_speed": 1.0
    }


def tts_elevenlabs(text: str) -> bytes | None:
    """
    TTS request using external config files for consistency.
    """
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        print("[diag] skipping TTS; missing ELEVEN_API_KEY/VOICE_ID or empty text")
        return None

    # Load settings from config files
    voice_config = load_voice_settings()
    
    # Enhanced sanitization for mid-sentence consistency
    text = sanitize_for_tts(text)
    
    # Debug output
    print(f"[diag] Using model: {voice_config['model_id']}")
    print(f"[diag] Voice settings: {voice_config['voice_settings']}")
    print(f"[diag] Sanitized text length: {len(text)} chars")
    
    base = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    url = f"{base}?output_format=mp3_44100_128"

    payload = {
        "text": text,
        "model_id": voice_config["model_id"],
        "voice_settings": voice_config["voice_settings"],
        "voice_speed": voice_config["voice_speed"]
    }

    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
        if r.status_code >= 400:
            print(f"[warn] ElevenLabs error {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return None
        print(f"[diag] ElevenLabs success: {len(r.content)} bytes")
        return r.content
    except requests.exceptions.Timeout:
        print("[warn] ElevenLabs request timed out", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[warn] ElevenLabs request failed: {e}", file=sys.stderr)
        return None


def sanitize_for_tts(s: str) -> str:
    """Enhanced TTS sanitization to prevent mid-sentence slowdowns."""
    import re
    
    # 1. Handle numbers that cause TTS to slow down
    s = re.sub(r'\b(\d{1,3}),(\d{3})\b', r'\1 thousand \2', s)  # 1,500 -> 1 thousand 500
    s = re.sub(r'\$(\d+)\.(\d{2})\b', r'\1 dollars and \2 cents', s)  # $15.99
    s = re.sub(r'\$(\d+)\b', r'\1 dollars', s)  # $15
    
    # 2. Handle dates and times
    s = re.sub(r'\b(\d{1,2}):(\d{2})\s*(AM|PM)\b', r'\1 \2 \3', s, flags=re.IGNORECASE)
    
    # 3. Simplify complex punctuation within sentences
    replacements = [
        ("—", " "),            # em dash (no pause needed)
        ("–", " "),            # en dash
        ("...", "."),          # multiple periods
        (" / ", " or "),       # slashes
        (" & ", " and "),      # ampersands
        ("'", "'"),            # normalize apostrophes
        (""", '"'),            # normalize quotes
        (""", '"'),            # normalize quotes
        (")", " "),            # Remove closing parens
        (";", ","),            # Semicolons often cause hesitation
    ]
    
    for old, new in replacements:
        s = s.replace(old, new)
    
    # 4. Remove parenthetical content that causes hesitation
    s = re.sub(r'\([^)]*', '', s)  # Remove opening paren and everything after
    
    # 5. Handle abbreviations that get elongated - using your existing logic but enhanced
    s = s.replace("MBTA", "M B T A")  # Space them out
    s = s.replace("BPL", "B P L")
    
    # Additional Boston-specific terms
    boston_terms = {
        "MIT": "M I T",
        "BU": "Boston University",
        "BC": "Boston College", 
        "Mass Pike": "Massachusetts Turnpike",
        "Storrow": "Storrow Drive",
        "Fenway": "Fenway Park",
    }
    
    for term, replacement in boston_terms.items():
        s = re.sub(rf'\b{term}\b', replacement, s, flags=re.IGNORECASE)
    
    # 6. Handle Boston place names that cause issues
    problem_words = {
        "Quincy": "Quin-see",
        "Worcester": "Woo-ster", 
        "Gloucester": "Gloss-ter",
        "Leominster": "Lemm-in-ster",
    }
    
    for word, pronunciation in problem_words.items():
        s = re.sub(rf'\b{word}\b', pronunciation, s, flags=re.IGNORECASE)
    
    # 7. Clean up spacing
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\s*,\s*', ', ', s)    
    s = re.sub(r'\s*\.\s*', '. ', s)   
    
    return s.strip()
