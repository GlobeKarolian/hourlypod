def tts_elevenlabs(text: str) -> bytes | None:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID or not text.strip():
        print("[diag] skipping TTS; missing ELEVEN_API_KEY/VOICE_ID or empty text")
        return None

    settings = _load_voice_settings()
    
    # Use clean URL without streaming parameters
    base_url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    
    print(f"[diag] ElevenLabs URL: {base_url}")

    payload = {
        "text": text,
        "model_id": settings.get("model_id", "eleven_multilingual_v2"),
        "voice_settings": {
            "stability": settings.get("stability", 0.72),
            "similarity_boost": settings.get("similarity_boost", 0.90),
            "style": settings.get("style", 0.25),
            "use_speaker_boost": bool(settings.get("use_speaker_boost", True)),
            # Move voice_speed inside voice_settings
            "voice_speed": settings.get("voice_speed", 1.0)  # Also try 1.0 instead of 1.04
        }
    }
    
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }

    print(f"[diag] Payload: {json.dumps(payload, indent=2)}")
    
    try:
        r = requests.post(base_url, headers=headers, json=payload, timeout=180)
        
        if r.status_code >= 400:
            print(f"[warn] ElevenLabs error {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return None
            
        print(f"[diag] ElevenLabs success: {len(r.content)} bytes received")
        return r.content
        
    except requests.exceptions.Timeout:
        print("[warn] ElevenLabs request timed out", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[warn] ElevenLabs request failed: {e}", file=sys.stderr)
        return None
