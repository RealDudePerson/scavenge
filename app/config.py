import os
import yaml
import datetime
from datetime import timezone

DATABASE_URL = "sqlite+aiosqlite:///./scavenge.db"

UPLOADS_DIR   = os.path.join(os.getcwd(), "uploads")
ORIGINALS_DIR = os.path.join(UPLOADS_DIR, "originals")
THUMB_DIR     = os.path.join(UPLOADS_DIR, "thumb")
DISPLAY_DIR   = os.path.join(UPLOADS_DIR, "display")
HUNTS_DIR     = os.path.join(os.getcwd(), "hunts")
TEAMS_DIR     = os.path.join(os.getcwd(), "teams")

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(ORIGINALS_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)
os.makedirs(DISPLAY_DIR, exist_ok=True)
os.makedirs(HUNTS_DIR, exist_ok=True)
os.makedirs(TEAMS_DIR, exist_ok=True)


def _load_cfg():
    try:
        with open("admin_config.yaml", "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def get_admin_password():
    return str(_load_cfg().get("admin_password", "")).strip()


def get_openai_config():
    cfg = _load_cfg()
    return {
        "api_key": os.environ.get("OPENAI_API_KEY") or cfg.get("openai_api_key", ""),
        "model": cfg.get("openai_model", "gpt-4o-mini"),
        "base_url": cfg.get("openai_base_url", "https://api.openai.com/v1"),
    }


def get_max_photo_age_hours() -> float:
    return float(_load_cfg().get("max_photo_age_hours", 4.0))


def get_hunt_state() -> dict:
    """Global hunt timer state.

    Keys:
        is_open: bool — True if hunt_ends_at is set and still in the future
        ends_at: datetime | None — timezone-aware end timestamp (None if not running)
        duration_minutes: int — default duration for "Start Hunt"
        seconds_remaining: int — 0 if closed
    """
    cfg = _load_cfg()
    ends_str = (cfg.get("hunt_ends_at") or "").strip()
    duration = int(cfg.get("hunt_duration_minutes", 160))

    if not ends_str:
        return {
            "is_open": False,
            "ends_at": None,
            "duration_minutes": duration,
            "seconds_remaining": 0,
        }

    try:
        ends_at = datetime.datetime.fromisoformat(ends_str.replace("Z", "+00:00"))
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return {
            "is_open": False,
            "ends_at": None,
            "duration_minutes": duration,
            "seconds_remaining": 0,
        }

    now = datetime.datetime.now(timezone.utc)
    remaining = max(0, int((ends_at - now).total_seconds()))

    return {
        "is_open": remaining > 0,
        "ends_at": ends_at,
        "duration_minutes": duration,
        "seconds_remaining": remaining,
    }


def set_hunt_ends_at(iso_timestamp: str):
    """Update hunt_ends_at in admin_config.yaml in place, preserving comments and key order."""
    path = "admin_config.yaml"
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    value = "'" + iso_timestamp.replace("'", "''") + "'"
    new_line = f"hunt_ends_at: {value}\n"
    for i, line in enumerate(lines):
        if line.lstrip().startswith("hunt_ends_at:"):
            lines[i] = new_line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(new_line)

    with open(path, "w") as f:
        f.writelines(lines)