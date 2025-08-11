# Boston Briefing (GPT-only)

This repo generates a short, factual Boston news briefing (MP3) using a prompt (`prompt.txt`) and GPT. It then publishes a GitHub Pages site with a player and a list of past episodes.

## Setup
1. Create a public GitHub repo and upload these files at the repo root.
2. In **Settings → Pages**, set **Source: GitHub Actions**.
3. In **Settings → Secrets and variables → Actions**, add:
   - `PUBLIC_BASE_URL` — e.g. `https://<user>.github.io/<repo>`
   - `OPENAI_API_KEY`
   - Optional: `OPENAI_MODEL` (default `gpt-4o`), `ELEVEN_API_KEY`, `ELEVEN_VOICE_ID`
4. Edit `prompt.txt` whenever you want to change the style.

## Run
- Go to **Actions → Build & Deploy → Run workflow**.
- The site will appear at your `PUBLIC_BASE_URL` and include a player + archive.

## Notes
- If GPT or TTS fails, the run still completes with a friendly fallback MP3 message.
