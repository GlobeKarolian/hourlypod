# api.py - Simple API endpoint for iOS app
import os, sys, json, datetime as dt
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
from zoneinfo import ZoneInfo

# Import your existing functions
from main import (
    fetch_items, dedupe, build_notes, rewrite_with_openai,
    sanitize_for_tts, tts_elevenlabs, boston_now
)

class APIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Handle GET requests"""
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == '/api/generate':
            self.handle_generate()
        elif parsed.path == '/api/episodes':
            self.handle_episodes()
        elif parsed.path == '/api/health':
            self.handle_health()
        else:
            self.send_error(404, "Not Found")
    
    def do_OPTIONS(self):
        """Handle CORS preflight"""
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()
    
    def send_cors_headers(self):
        """Send CORS headers for iOS app"""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
    
    def handle_health(self):
        """Health check endpoint"""
        self.send_response(200)
        self.send_cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        
        response = {
            "status": "healthy",
            "timestamp": dt.datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "service": "Boston Briefing API"
        }
        self.wfile.write(json.dumps(response).encode())
    
    def handle_episodes(self):
        """Return list of available episodes"""
        self.send_response(200)
        self.send_cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        
        # Look for existing episodes
        public_dir = Path("public")
        episodes_dir = public_dir / "episodes"
        episodes = []
        
        if episodes_dir.exists():
            for mp3_file in episodes_dir.glob("*.mp3"):
                # Extract date from filename like "boston-briefing-2025-08-12.mp3"
                date_str = mp3_file.stem.replace("boston-briefing-", "")
                try:
                    date_obj = dt.datetime.strptime(date_str, "%Y-%m-%d")
                    episodes.append({
                        "id": date_str,
                        "title": f"Boston Briefing – {date_obj.strftime('%B %d, %Y')}",
                        "date": date_str,
                        "audioURL": f"/episodes/{mp3_file.name}",
                        "duration": 180  # Estimate
                    })
                except ValueError:
                    continue
        
        # Sort by date, newest first
        episodes.sort(key=lambda x: x["date"], reverse=True)
        
        response = {
            "episodes": episodes[:10],  # Last 10 episodes
            "total": len(episodes)
        }
        self.wfile.write(json.dumps(response).encode())
    
    def handle_generate(self):
        """Generate a new episode"""
        try:
            self.send_response(200)
            self.send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            # Step 1: Fetch news
            print("[API] Fetching news...")
            raw_items = fetch_items()
            deduped_items = dedupe(raw_items)
            
            if not deduped_items:
                raise Exception("No news items found")
            
            # Step 2: Build notes
            print("[API] Building story notes...")
            notes = build_notes(deduped_items)
            
            if not notes:
                raise Exception("No valid story notes generated")
            
            # Step 3: Generate script
            print("[API] Generating script...")
            prompt_path = Path("prompt.txt")
            prompt_text = ""
            if prompt_path.exists():
                prompt_text = prompt_path.read_text(encoding="utf-8")
            
            script = rewrite_with_openai(prompt_text, notes)
            
            if not script:
                raise Exception("Failed to generate script")
            
            # Step 4: Generate audio (optional)
            audio_data = None
            audio_url = None
            
            if os.getenv("ELEVEN_API_KEY") and os.getenv("ELEVEN_VOICE_ID"):
                print("[API] Generating audio...")
                sanitized_script = sanitize_for_tts(script)
                audio_data = tts_elevenlabs(sanitized_script)
                
                if audio_data:
                    # Save audio file
                    today = dt.datetime.now(ZoneInfo("America/New_York"))
                    date_str = today.strftime("%Y-%m-%d")
                    
                    public_dir = Path("public")
                    episodes_dir = public_dir / "episodes"
                    episodes_dir.mkdir(parents=True, exist_ok=True)
                    
                    audio_filename = f"boston-briefing-{date_str}.mp3"
                    audio_path = episodes_dir / audio_filename
                    audio_path.write_bytes(audio_data)
                    
                    audio_url = f"/episodes/{audio_filename}"
                    print(f"[API] Audio saved: {audio_filename}")
            
            # Step 5: Create response
            today = dt.datetime.now(ZoneInfo("America/New_York"))
            episode = {
                "id": today.strftime("%Y-%m-%d"),
                "title": f"Boston Briefing – {today.strftime('%B %d, %Y')}",
                "date": today.strftime("%Y-%m-%d"),
                "script": script,
                "audioURL": audio_url,
                "duration": 180,
                "generatedAt": today.isoformat()
            }
            
            response = {
                "success": True,
                "episode": episode,
                "message": "Episode generated successfully"
            }
            
            self.wfile.write(json.dumps(response).encode())
            print("[API] Episode generation complete")
            
        except Exception as e:
            print(f"[API ERROR] {e}")
            
            self.send_response(500)
            self.send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            response = {
                "success": False,
                "error": str(e),
                "message": "Failed to generate episode"
            }
            self.wfile.write(json.dumps(response).encode())

def start_api_server(port=8000):
    """Start the API server"""
    server = HTTPServer(('', port), APIHandler)
    print(f"[API] Starting server on port {port}")
    print(f"[API] Health check: http://localhost:{port}/api/health")
    print(f"[API] Generate episode: http://localhost:{port}/api/generate")
    print(f"[API] List episodes: http://localhost:{port}/api/episodes")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[API] Server stopped")
        server.shutdown()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    start_api_server(port)