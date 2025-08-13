# create_api.py - Create static JSON API endpoints
import json
import os
from pathlib import Path
import datetime as dt
from zoneinfo import ZoneInfo

def create_episodes_api():
    """Create episodes.json API endpoint"""
    # Get episodes from public/episodes directory
    public_dir = Path("public")
    episodes_dir = public_dir / "episodes"
    api_dir = public_dir / "api"
    api_dir.mkdir(exist_ok=True)
    
    episodes = []
    
    if episodes_dir.exists():
        for mp3_file in episodes_dir.glob("*.mp3"):
            # Extract date from filename like "boston-briefing-2025-08-12.mp3"
            date_str = mp3_file.stem.replace("boston-briefing-", "")
            try:
                date_obj = dt.datetime.strptime(date_str, "%Y-%m-%d")
                
                # Try to get script content from a text file if it exists
                script_file = episodes_dir / f"{mp3_file.stem}.txt"
                script = ""
                if script_file.exists():
                    script = script_file.read_text(encoding="utf-8")
                
                episodes.append({
                    "id": date_str,
                    "title": f"Boston Briefing – {date_obj.strftime('%B %d, %Y')}",
                    "date": date_str,
                    "script": script,
                    "audioURL": f"{os.getenv('PUBLIC_BASE_URL', '')}/episodes/{mp3_file.name}",
                    "duration": 180,  # 3 minutes estimate
                    "generatedAt": date_obj.isoformat()
                })
            except ValueError:
                continue
    
    # Sort by date, newest first
    episodes.sort(key=lambda x: x["date"], reverse=True)
    
    # Create episodes API response
    api_response = {
        "episodes": episodes[:10],  # Last 10 episodes
        "total": len(episodes),
        "lastUpdated": dt.datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "status": "success"
    }
    
    # Write to public/api/episodes.json
    episodes_api_path = api_dir / "episodes.json"
    with open(episodes_api_path, "w", encoding="utf-8") as f:
        json.dump(api_response, f, indent=2)
    
    print(f"✅ Created episodes API: {episodes_api_path}")
    print(f"   Episodes: {len(episodes)}")
    return episodes

def create_generate_api():
    """Create a simple generate endpoint that triggers a workflow"""
    public_dir = Path("public")
    api_dir = public_dir / "api"
    api_dir.mkdir(exist_ok=True)
    
    # For now, this will just return instructions to manually trigger
    generate_response = {
        "message": "To generate a new episode, go to your GitHub repo and run the workflow manually",
        "instructions": [
            "1. Go to your GitHub repo",
            "2. Click 'Actions' tab", 
            "3. Click 'Build & Deploy'",
            "4. Click 'Run workflow'",
            "5. New episode will appear in about 2-3 minutes"
        ],
        "workflow_url": "https://github.com/YOUR_USERNAME/YOUR_REPO/actions",
        "status": "info"
    }
    
    generate_api_path = api_dir / "generate.json"
    with open(generate_api_path, "w", encoding="utf-8") as f:
        json.dump(generate_response, f, indent=2)
    
    print(f"✅ Created generate API: {generate_api_path}")

def create_health_api():
    """Create health check endpoint"""
    public_dir = Path("public")
    api_dir = public_dir / "api"
    api_dir.mkdir(exist_ok=True)
    
    health_response = {
        "status": "healthy",
        "service": "Boston Briefing Static API",
        "timestamp": dt.datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "version": "1.0"
    }
    
    health_api_path = api_dir / "health.json"
    with open(health_api_path, "w", encoding="utf-8") as f:
        json.dump(health_response, f, indent=2)
    
    print(f"✅ Created health API: {health_api_path}")

def save_current_script():
    """Save the current episode script as a text file"""
    # This would be called after generating an episode
    # to save the script alongside the MP3
    today = dt.datetime.now(ZoneInfo("America/New_York"))
    date_str = today.strftime("%Y-%m-%d")
    
    # Try to find the script from the main.py output or a temp file
    script_content = "Script content would be saved here"
    
    public_dir = Path("public")
    episodes_dir = public_dir / "episodes"
    script_file = episodes_dir / f"boston-briefing-{date_str}.txt"
    
    # This would normally save the actual script
    # For now, just create a placeholder
    print(f"📝 Script would be saved to: {script_file}")

if __name__ == "__main__":
    print("🔧 Creating static JSON API endpoints...")
    
    episodes = create_episodes_api()
    create_generate_api()
    create_health_api()
    
    print(f"\n✅ Static API created successfully!")
    print(f"📍 Episodes endpoint: /api/episodes.json")
    print(f"📍 Generate endpoint: /api/generate.json") 
    print(f"📍 Health endpoint: /api/health.json")
    print(f"\n🌐 Base URL: {os.getenv('PUBLIC_BASE_URL', 'YOUR_GITHUB_PAGES_URL')}")
