import os
import yaml
import datetime
from datetime import timezone, timedelta

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


def get_admin_password():
    with open("admin_config.yaml", "r") as f:
        cfg = yaml.safe_load(f)
    return str(cfg.get("admin_password", "")).strip()


def get_openai_config():
    """Load OpenAI-compatible endpoint configuration.
    API key lookup order: env var OPENAI_API_KEY -> admin_config.yaml -> empty string.
    Model, base URL, and daily budget are read from admin_config.yaml."""
    try:
        with open("admin_config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    return {
        "api_key": os.environ.get("OPENAI_API_KEY") or cfg.get("openai_api_key", ""),
        "model": cfg.get("openai_model", "gpt-4o-mini"),
        "base_url": cfg.get("openai_base_url", "https://api.openai.com/v1"),
        "daily_budget_usd": float(cfg.get("openai_daily_budget_usd", 5.0)),
    }


def get_max_photo_age_hours() -> float:
    """Maximum age of an uploaded photo based on its EXIF DateTimeOriginal.
    Read from admin_config.yaml (key: max_photo_age_hours). Defaults to 4."""
    try:
        with open("admin_config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    return float(cfg.get("max_photo_age_hours", 4.0))


def get_hunt_state() -> dict:
    """Return the current global hunt timer state.

    Keys:
        is_open: bool — True if the hunt is currently accepting submissions
        is_expired: bool — True if hunt_ends_at was set but is in the past
        ends_at: datetime | None — timezone-aware end timestamp
        duration_minutes: int — default duration for "Start Hunt"
        seconds_remaining: int — 0 if closed/expired
        ends_at_display: str — human-friendly "M:SS" or "H:MM:SS" remaining (empty if closed)
    """
    try:
        with open("admin_config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}

    ends_str = (cfg.get("hunt_ends_at") or "").strip()
    duration = int(cfg.get("hunt_duration_minutes", 60))

    if not ends_str:
        return {
            "is_open": False,
            "is_expired": False,
            "ends_at": None,
            "duration_minutes": duration,
            "seconds_remaining": 0,
            "ends_at_display": "",
        }

    try:
        ends_at = datetime.datetime.fromisoformat(ends_str.replace("Z", "+00:00"))
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return {
            "is_open": False,
            "is_expired": False,
            "ends_at": None,
            "duration_minutes": duration,
            "seconds_remaining": 0,
            "ends_at_display": "",
        }

    now = datetime.datetime.now(timezone.utc)
    remaining = (ends_at - now).total_seconds()
    is_expired = remaining <= 0
    secs = max(0, int(remaining))

    # Format as M:SS or H:MM:SS
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h > 0:
        formatted = f"{h}:{m:02d}:{s:02d}"
    else:
        formatted = f"{m}:{s:02d}"

    return {
        "is_open": not is_expired,
        "is_expired": is_expired,
        "ends_at": ends_at,
        "duration_minutes": duration,
        "seconds_remaining": secs,
        "ends_at_display": formatted,
    }


def set_hunt_ends_at(iso_timestamp: str):
    """Write the hunt_ends_at field to admin_config.yaml."""
    with open("admin_config.yaml", "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["hunt_ends_at"] = iso_timestamp
    with open("admin_config.yaml", "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False)