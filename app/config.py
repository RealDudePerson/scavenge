import os

# Database
DATABASE_URL = "sqlite+aiosqlite:///./scavenge.db"

# Directories
UPLOADS_DIR = os.path.join(os.getcwd(), "uploads")
HUNTS_DIR = os.path.join(os.getcwd(), "hunts")
TEAMS_DIR = os.path.join(os.getcwd(), "teams")

# Create directories
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(HUNTS_DIR, exist_ok=True)
os.makedirs(TEAMS_DIR, exist_ok=True)
