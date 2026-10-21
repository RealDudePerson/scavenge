from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request, responses
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from sqlalchemy.future import select
from sqlalchemy import func
from .database import async_session, init_db
from .models import Hunt, HuntItem, Team, TeamPosition, Submission, Base
from .config import HUNTS_DIR, TEAMS_DIR, ORIGINALS_DIR, THUMB_DIR, DISPLAY_DIR, get_admin_password, get_max_photo_age_hours, get_hunt_state, set_hunt_ends_at
from .ai_review import review_image
from .security import hash_passphrase, verify_passphrase
import logging
import json
import math
import re
import time
import yaml
import os
import uuid
import datetime
from datetime import timezone, timedelta
from PIL import Image, ImageOps
import pillow_heif

pillow_heif.register_heif_opener()

logger = logging.getLogger("uvicorn.error")

_SESSION_SECRET = os.environ.get("SESSION_SECRET")
if not _SESSION_SECRET:
    _SESSION_SECRET = uuid.uuid4().hex
    logger.warning("SESSION_SECRET is not set — using a random key; all logins will be lost on restart.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _refresh_theme()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=_SESSION_SECRET)
templates = Jinja2Templates(directory="templates")
app.mount("/uploads/thumb", StaticFiles(directory="uploads/thumb"), name="uploads-thumb")
app.mount("/uploads/display", StaticFiles(directory="uploads/display"), name="uploads-display")
app.mount("/static", StaticFiles(directory="static"), name="static")


MAX_ATTEMPTS_PER_ITEM = 10
CONFIDENCE_THRESHOLD = 0.7

DEFAULT_THEME = {
    "primary":      "#FF6B9D",
    "secondary":    "#FEC868",
    "accent":       "#7AC74F",
    "background":   "#FFF8F0",
    "surface":      "#FFFFFF",
    "text":         "#2D3142",
    "text_muted":   "#6C757D",
    "success":      "#06A77D",
    "warning":      "#E63946",
    "info":         "#4A90E2",
}

_ACTIVE_THEME = DEFAULT_THEME.copy()
# The "active" hunt is the first hunt by id — the same one whose theme is
# applied. Kept in the module cache so the dashboard can scope its item list
# without an extra per-request query (§1.4).
_ACTIVE_HUNT_ID = None


def _ctx(request: Request, **extra) -> dict:
    """Base context for every rendered template."""
    is_admin = request.session.get("is_admin") == True
    is_team = request.session.get("team_id") is not None
    return {
        "is_admin": is_admin,
        "is_team_member": is_team,
        "theme": _active_theme(),
        "hunt_state": get_hunt_state(),
        **extra,
    }


async def _refresh_theme():
    """Load the first hunt's theme merged over DEFAULT_THEME into the module cache."""
    global _ACTIVE_THEME, _ACTIVE_HUNT_ID
    try:
        async with async_session() as session:
            hunt = (await session.execute(select(Hunt).order_by(Hunt.id.asc()))).scalars().first()
        if hunt:
            _ACTIVE_HUNT_ID = hunt.id
        else:
            _ACTIVE_HUNT_ID = None
        if hunt and hunt.theme_json:
            overrides = json.loads(hunt.theme_json)
            merged = DEFAULT_THEME.copy()
            merged.update({k: v for k, v in overrides.items() if v})
            _ACTIVE_THEME = merged
        else:
            _ACTIVE_THEME = DEFAULT_THEME.copy()
    except Exception:
        logger.exception("Failed to load active theme; using defaults")
        _ACTIVE_THEME = DEFAULT_THEME.copy()


def _active_theme() -> dict:
    """Cached active theme — populated at startup and refreshed on hunt/reset changes."""
    return _ACTIVE_THEME


def require_admin(request: Request):
    if request.session.get("is_admin") != True:
        raise HTTPException(status_code=403, detail="Admin access required")


def require_team(request: Request):
    if not request.session.get("team_id"):
        raise HTTPException(status_code=403, detail="Team login required")


def _cleanup_files(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _thumb_base(submission) -> str:
    return os.path.splitext(os.path.basename(submission.thumbnail_path or ""))[0]


# --- Upload filename sanitisation (§1.3) ---

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_FILENAME_LENGTH = 100


def sanitize_filename(filename, max_length: int = _MAX_FILENAME_LENGTH) -> str:
    """Reduce a client-supplied filename to a safe basename (§1.3).

    Strips any directory component, keeps only ``[A-Za-z0-9._-]``, removes
    leading dots, and caps the length. The result can never contain a path
    separator or a ``..`` component, so it cannot escape the upload directory.
    """
    if not filename:
        return "upload"
    if isinstance(filename, bytes):
        filename = filename.decode("utf-8", errors="ignore")
    # Normalise Windows separators, then keep only the final path component.
    name = str(filename).replace("\\", "/").split("/")[-1]
    name = name.replace("\x00", "").strip()
    name = _SAFE_FILENAME_RE.sub("_", name)
    name = name.lstrip(".")  # no hidden or relative names ("." / "..")
    if not name:
        name = "upload"
    if len(name) > max_length:
        root, ext = os.path.splitext(name)
        ext = ext[:10]
        name = root[: max_length - len(ext)] + ext
    return name


def build_original_filename(client_filename) -> str:
    """uuid4-prefixed, sanitised filename — the prefix is retained (§1.3)."""
    return f"{uuid.uuid4().hex}_{sanitize_filename(client_filename)}"


# --- Coordinate judging (§2) ---

def _opt_float(value, default=None):
    """Coerce to a finite float, else return ``default``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _dms_to_degrees(dms):
    """EXIF (deg, min, sec) rationals -> decimal degrees, or ``None``."""
    if dms is None:
        return None
    try:
        parts = list(dms)
    except TypeError:
        return None
    if len(parts) < 2:
        return None
    degrees = _opt_float(parts[0])
    minutes = _opt_float(parts[1])
    seconds = _opt_float(parts[2]) if len(parts) > 2 else 0.0
    if degrees is None or minutes is None or seconds is None:
        return None
    return degrees + minutes / 60.0 + seconds / 3600.0


def parse_gps(img) -> tuple[float, float] | None:
    """Read GPS lat/lon from an image's EXIF, server-side (§2.2).

    Returns ``(lat, lon)`` or ``None``. Values are range-validated; NaN/Inf and
    out-of-range values are rejected as if the GPS were absent. An exactly-zero
    pair ``(0.0, 0.0)`` — Null Island, open ocean — is the redaction signature
    Android's ``MediaProvider`` writes for apps without ``ACCESS_MEDIA_LOCATION``,
    and is likewise treated as absent. Only *both* components zero is the
    sentinel: a genuine fix on the equator (``0.0, -116.5``) or prime meridian
    (``34.1, 0.0``) still parses. Never reads a multipart field — the stored
    original is the only trusted source.
    """
    from PIL import ExifTags
    try:
        exif = img.getexif()
        if not exif:
            return None
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo) or {}
    except Exception:
        return None
    if not gps:
        return None

    lat = _dms_to_degrees(gps.get(2))
    lon = _dms_to_degrees(gps.get(4))
    if lat is None or lon is None:
        return None

    lat_ref = gps.get(1)
    lon_ref = gps.get(3)
    if isinstance(lat_ref, bytes):
        lat_ref = lat_ref.decode("utf-8", errors="ignore")
    if isinstance(lon_ref, bytes):
        lon_ref = lon_ref.decode("utf-8", errors="ignore")
    if lat_ref and str(lat_ref).strip().upper().startswith("S"):
        lat = -lat
    if lon_ref and str(lon_ref).strip().upper().startswith("W"):
        lon = -lon

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None

    # §2.2 (null-island) — an exactly-zero pair is the Android redaction
    # signature, not a fix: return None so downstream reports "missing" rather
    # than a false Gulf-of-Guinea distance. Both components must be zero; a
    # coordinate merely *containing* a zero is a real location and still parses.
    if lat == 0.0 and lon == 0.0:
        logger.info("GPS null-island sentinel (0.0, 0.0) in EXIF — treating location as absent")
        return None

    return lat, lon


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres — no geospatial dependency (§2.3)."""
    earth_radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius_m * math.asin(min(1.0, math.sqrt(a)))


def score_proximity(item, coords):
    """Map a coordinate to ``(location_status, proximity_points_awarded, distance_m)``.

    Boundaries are inclusive: ``d == radius`` is a hit and
    ``d == radius × near_multiplier`` is a near. See §2.3.
    """
    mode = item.proximity_mode or "bonus"
    if item.target_lat is None or item.target_lon is None:
        return ("no_target", 0, None)
    if mode == "off":
        return ("off", 0, None)
    if coords is None:
        return ("missing", 0, None)

    lat, lon = coords
    distance = haversine_m(lat, lon, item.target_lat, item.target_lon)
    radius = item.radius_meters if item.radius_meters is not None else 100.0
    near_multiplier = item.near_multiplier if item.near_multiplier is not None else 2.0
    points = item.proximity_points or 0
    if distance <= radius:
        return ("hit", points, distance)
    if distance <= radius * near_multiplier:
        return ("near", points // 2, distance)
    return ("far", 0, distance)


def proximity_points_for_status(item, status):
    """Proximity points implied by an already-recorded ``location_status``.

    Used on re-review, where the stored status (not raw coordinates) is the
    source of truth. Mirrors :func:`score_proximity`'s hit/near mapping.
    """
    points = item.proximity_points or 0
    if status == "hit":
        return points
    if status == "near":
        return points // 2
    return 0


def _location_note(status: str, proximity_points_awarded: int) -> str:
    """Human-readable location sentence for the /result message (§2.4)."""
    if status == "hit":
        return f" Location: on target (+{proximity_points_awarded} proximity points)."
    if status == "near":
        return f" Location: near the target (+{proximity_points_awarded} proximity points)."
    if status == "far":
        return " Location: too far from the target for proximity points."
    if status == "missing":
        return " Location unavailable — no proximity points."
    return ""


# --- Login rate limiting (§3.3) ---

_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_ATTEMPTS = 10
_login_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_rate_limited(request: Request) -> bool:
    """True when this client has exhausted its attempts in the window.

    A simple in-memory fixed-window counter keyed by client IP. Counting
    continues on success; logins are rare enough that this is acceptable.
    """
    now = time.monotonic()
    ip = _client_ip(request)
    attempts = _login_attempts.setdefault(ip, [])
    cutoff = now - _LOGIN_WINDOW_SECONDS
    attempts[:] = [t for t in attempts if t > cutoff]
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        return True
    attempts.append(now)
    return False


# --- Position rate limiting (§3) ---

_POSITION_WINDOW_SECONDS = 60
_POSITION_MAX_PER_WINDOW = 60  # 60 req/min per IP — generous for phone polling
_position_attempts: dict[str, list[float]] = {}


def _position_rate_limited(request: Request) -> bool:
    """True when this client has exhausted its position-update limit."""
    now = time.monotonic()
    ip = _client_ip(request)
    attempts = _position_attempts.setdefault(ip, [])
    cutoff = now - _POSITION_WINDOW_SECONDS
    attempts[:] = [t for t in attempts if t > cutoff]
    if len(attempts) >= _POSITION_MAX_PER_WINDOW:
        return True
    attempts.append(now)
    return False


_STALE_THRESHOLD_SECONDS = 60  # positions older than this are marked stale


# --- Auth Endpoints ---

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return responses.RedirectResponse(url="/", status_code=303)


@app.get("/admin/login")
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "admin_login.html", _ctx(request))


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if password.strip() == get_admin_password():
        request.session["is_admin"] = True
        return {"message": "Login successful", "redirect": "/admin/dashboard"}
    raise HTTPException(status_code=401, detail="Invalid password")


@app.post("/admin/reset", dependencies=[Depends(require_admin)])
async def reset_db():
    from .database import engine
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        for upload_dir in (ORIGINALS_DIR, THUMB_DIR, DISPLAY_DIR):
            if os.path.isdir(upload_dir):
                for filename in os.listdir(upload_dir):
                    file_path = os.path.join(upload_dir, filename)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
        await _refresh_theme()
        return {"message": "All progress and teams reset."}
    except Exception as e:
        logger.exception("Reset failed")
        return {"message": f"Reset failed: {str(e)}"}, 500


@app.post("/admin/cleanup-orphans", dependencies=[Depends(require_admin)])
async def cleanup_orphans():
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Submission.photo_path, Submission.thumbnail_path, Submission.display_path)
            )
            referenced = set()
            for photo, thumb, display in result.all():
                if photo:   referenced.add(photo)
                if thumb:   referenced.add(thumb)
                if display: referenced.add(display)

        deleted = 0
        for upload_dir in (ORIGINALS_DIR, THUMB_DIR, DISPLAY_DIR):
            if not os.path.isdir(upload_dir):
                continue
            for filename in os.listdir(upload_dir):
                file_path = os.path.join(upload_dir, filename)
                if os.path.isfile(file_path) and file_path not in referenced:
                    os.remove(file_path)
                    deleted += 1

        return {"message": f"Cleaned up {deleted} orphan file(s)."}
    except Exception as e:
        logger.exception("Orphan cleanup failed")
        return {"message": f"Cleanup failed: {str(e)}"}, 500


@app.post("/admin/load-hunts", dependencies=[Depends(require_admin)])
async def load_hunts():
    skipped = []
    loaded = []
    for filename in os.listdir(HUNTS_DIR):
        if not filename.endswith(".yaml"):
            continue
        with open(os.path.join(HUNTS_DIR, filename), "r") as f:
            data = yaml.safe_load(f)
        if not data or "items" not in data:
            continue
        async with async_session() as session:
            result = await session.execute(select(Hunt).where(Hunt.name == data['name']))
            if result.scalar():
                continue
            theme = data.get('theme') or {}
            hunt = Hunt(
                name=data['name'],
                theme_json=json.dumps(theme),
            )
            session.add(hunt)
            await session.commit()
            for item_data in data['items']:
                required = item_data.get('required_properties')
                if not required or not isinstance(required, list) or len(required) == 0:
                    logger.warning(f"Skipping item '{item_data.get('name', '?')}' in '{data['name']}': missing required_properties")
                    skipped.append(item_data.get('name', '?'))
                    continue
                proximity_mode = item_data.get('proximity_mode') or 'bonus'
                if proximity_mode not in ('off', 'bonus', 'gate'):
                    proximity_mode = 'bonus'
                item = HuntItem(
                    hunt_id=hunt.id,
                    name=item_data['name'],
                    description=item_data.get('description', ''),
                    points=item_data.get('points', 0),
                    bonus_points=item_data.get('bonus_points', 0),
                    required_properties=json.dumps(required),
                    bonus_properties=json.dumps(item_data.get('bonus_properties', [])),
                    bonus_hint=item_data.get('bonus_hint'),
                    # Coordinate judging (§2.1) — optional; absent means no target.
                    target_lat=_opt_float(item_data.get('target_lat')),
                    target_lon=_opt_float(item_data.get('target_lon')),
                    radius_meters=_opt_float(item_data.get('radius_meters'), 100.0),
                    near_multiplier=_opt_float(item_data.get('near_multiplier'), 2.0),
                    proximity_points=int(item_data.get('proximity_points') or 0),
                    proximity_mode=proximity_mode,
                    zone_lat=_opt_float(item_data.get('zone_lat')),
                    zone_lon=_opt_float(item_data.get('zone_lon')),
                    zone_radius_meters=_opt_float(item_data.get('zone_radius_meters')),
                )
                session.add(item)
            await session.commit()
            loaded.append(data['name'])
    await _refresh_theme()
    return {"message": f"Hunts loaded. {len(loaded)} hunt(s), skipped {len(skipped)} item(s) without required_properties."}


@app.post("/admin/load-teams", dependencies=[Depends(require_admin)])
async def load_teams():
    with open(os.path.join(TEAMS_DIR, "teams.yaml"), "r") as f:
        data = yaml.safe_load(f)
    async with async_session() as session:
        for team_data in data['teams']:
            result = await session.execute(select(Team).where(Team.name == team_data['name']))
            if result.scalar():
                continue
            team = Team(name=team_data['name'], passphrase_hash=hash_passphrase(team_data['passphrase']))
            session.add(team)
        await session.commit()
    return {"message": "Teams loaded."}


@app.post("/admin/review-submission/{sub_id}", dependencies=[Depends(require_admin)])
async def admin_review_submission(sub_id: int):
    """Re-run the AI review on a specific submission. Useful for disputed cases."""
    async with async_session() as session:
        sub = (await session.execute(select(Submission).where(Submission.id == sub_id))).scalar_one_or_none()
        if not sub:
            raise HTTPException(404, "Submission not found")
        item = (await session.execute(select(HuntItem).where(HuntItem.id == sub.item_id))).scalar_one_or_none()
        if not item:
            raise HTTPException(404, "Item not found")

    required = json.loads(item.required_properties or "[]")
    bonus = json.loads(item.bonus_properties or "[]")
    review = await run_in_threadpool(
        review_image, sub.photo_path, item.name, item.description or "", required, bonus
    )

    all_required_met = (
        review["is_target"]
        and review["confidence"] >= CONFIDENCE_THRESHOLD
        and len(review["missed_required"]) == 0
    )
    all_bonus_met = all_required_met and len(review["missed_bonus"]) == 0 and len(bonus) > 0
    awarded_bonus = item.bonus_points if all_bonus_met else 0
    ai_points = item.points + awarded_bonus if all_required_met else 0
    # Proximity is a bonus on a valid find, never a consolation prize: it is
    # awarded only when the photo is approved. The stored location_status is
    # still the source of truth (§2.3) and is left untouched.
    proximity_points = proximity_points_for_status(item, sub.location_status) if all_required_met else 0
    points_awarded = ai_points + proximity_points
    verified = all_required_met
    if (item.proximity_mode or "bonus") == "gate":
        verified = all_required_met and sub.location_status in ("hit", "near")

    async with async_session() as session:
        sub = (await session.execute(select(Submission).where(Submission.id == sub_id))).scalar_one()
        sub.ai_review_result = json.dumps(review)
        sub.ai_bonus_awarded = awarded_bonus
        sub.points_awarded = points_awarded
        sub.proximity_points_awarded = proximity_points
        sub.verified = verified
        await session.commit()

    return {
        "verified": verified,
        "bonus_awarded": awarded_bonus,
        "points_awarded": points_awarded,
        "review": review,
    }


# --- Phase 2: Map API ---

def _blank_position_value(value) -> bool:
    """True when a client sent nothing meaningful for a coordinate."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


async def _read_position_payload(request: Request):
    """Read a position update from a JSON body *or* form encoding (§1).

    Both encodings are accepted so existing clients keep working. Returns
    ``(lat_raw, lon_raw, accuracy_raw, source)``.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object.")
        return (
            body.get("lat"),
            body.get("lon"),
            body.get("accuracy_m"),
            body.get("source") or "app",
        )
    # Read raw form data so we can validate empty strings ourselves.
    form = await request.form()
    return (
        form.get("lat"),
        form.get("lon"),
        form.get("accuracy_m"),
        form.get("source") or "app",
    )


def _coerce_position(lat_raw, lon_raw, accuracy_raw):
    """Validate a raw position payload into ``(lat, lon, accuracy)`` (§3).

    Must be non-empty, finite real numbers within range; anything else raises a
    400. ``accuracy`` is optional and becomes ``None`` when absent or invalid.
    """
    if _blank_position_value(lat_raw) or _blank_position_value(lon_raw):
        raise HTTPException(status_code=400, detail="lat and lon must be non-empty.")
    try:
        lat_f = float(lat_raw)
        lon_f = float(lon_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="lat and lon must be numeric.")
    if math.isnan(lat_f) or math.isinf(lat_f) or math.isnan(lon_f) or math.isinf(lon_f):
        raise HTTPException(status_code=400, detail="lat and lon must be finite.")
    if not (-90.0 <= lat_f <= 90.0):
        raise HTTPException(status_code=400, detail="lat out of range [-90, 90].")
    if not (-180.0 <= lon_f <= 180.0):
        raise HTTPException(status_code=400, detail="lon out of range [-180, 180].")

    acc = None
    if not _blank_position_value(accuracy_raw):
        try:
            acc = float(accuracy_raw)
        except (TypeError, ValueError):
            acc = None
        if acc is not None and (math.isnan(acc) or math.isinf(acc)):
            acc = None
    return lat_f, lon_f, acc


@app.post("/api/position")
async def update_position(request: Request, _: None = Depends(require_team)):
    """Record the team's live position for the map. Display only, never scored."""
    team_id = request.session.get("team_id")

    if _position_rate_limited(request):
        logger.warning(f"Position RATE-LIMITED for {_client_ip(request)}")
        raise HTTPException(status_code=429, detail="Too many position updates.")

    lat_raw, lon_raw, accuracy_raw, source = await _read_position_payload(request)
    lat_f, lon_f, acc = _coerce_position(lat_raw, lon_raw, accuracy_raw)

    now = datetime.datetime.now(timezone.utc)
    async with async_session() as session:
        # Prune old positions for this team: keep only the latest.
        old_positions = await session.execute(
            select(TeamPosition).where(TeamPosition.team_id == team_id)
        )
        for old in old_positions.scalars().all():
            await session.delete(old)

        pos = TeamPosition(
            team_id=team_id,
            lat=lat_f,
            lon=lon_f,
            accuracy_m=acc,
            recorded_at=now,
            source=source,
        )
        session.add(pos)
        await session.commit()

    return {"message": "Position updated."}


@app.get("/api/map")
async def team_map_data(request: Request, _: None = Depends(require_team)):
    """Grouped payload for the team map — items (display zone only), own position, own submissions.

    The true target is never included (§1). Items without zone_* are omitted entirely.
    """
    team_id = request.session.get("team_id")
    now = datetime.datetime.now(timezone.utc)
    async with async_session() as session:
        # Items: only those with display zone data, no true target.
        items_result = await session.execute(
            select(HuntItem).where(
                HuntItem.zone_lat.isnot(None),
                HuntItem.zone_lon.isnot(None),
            )
        )
        items = []
        for item in items_result.scalars().all():
            items.append({
                "id": item.id,
                "name": item.name,
                "points": item.points,
                "zone": {
                    "lat": item.zone_lat,
                    "lon": item.zone_lon,
                    "radius_m": item.zone_radius_meters or 100.0,
                },
            })

        # Own position.
        pos_result = await session.execute(
            select(TeamPosition).where(TeamPosition.team_id == team_id).order_by(TeamPosition.recorded_at.desc()).limit(1)
        )
        me = None
        pos = pos_result.scalars().first()
        if pos and pos.recorded_at:
            # recorded_at is stored as naive UTC per model default.
            pos_dt = pos.recorded_at.replace(tzinfo=timezone.utc) if pos.recorded_at.tzinfo is None else pos.recorded_at
            age = (now - pos_dt).total_seconds()
            me = {
                "team_id": team_id,
                "name": (await session.execute(select(Team).where(Team.id == team_id))).scalar_one().name,
                "lat": pos.lat,
                "lon": pos.lon,
                "accuracy_m": pos.accuracy_m,
                "age_seconds": int(age),
                "stale": age > _STALE_THRESHOLD_SECONDS,
            }

        # Own submissions with GPS.
        subs_result = await session.execute(
            select(Submission).where(
                Submission.team_id == team_id,
                Submission.gps_lat.isnot(None),
                Submission.gps_lon.isnot(None),
            ).order_by(Submission.submitted_at.desc())
        )
        submissions = []
        for sub in subs_result.scalars().all():
            age = (now - sub.submitted_at.replace(tzinfo=timezone.utc) if sub.submitted_at.tzinfo is None else sub.submitted_at).total_seconds() if sub.submitted_at else 0
            submissions.append({
                "item_id": sub.item_id,
                "lat": sub.gps_lat,
                "lon": sub.gps_lon,
                "status": sub.location_status,
                "age_seconds": int(age),
            })

    return {"items": items, "me": me, "submissions": submissions}


@app.get("/admin/map")
async def admin_map_data(request: Request, _: None = Depends(require_admin)):
    """Admin map payload — same shape as team map, plus all teams and true targets.
    
    Includes unapproved submissions.
    """
    now = datetime.datetime.now(timezone.utc)
    async with async_session() as session:
        # Items with true target.
        items_result = await session.execute(
            select(HuntItem).where(
                HuntItem.zone_lat.isnot(None),
                HuntItem.zone_lon.isnot(None),
            )
        )
        items = []
        for item in items_result.scalars().all():
            items.append({
                "id": item.id,
                "name": item.name,
                "points": item.points,
                "zone": {
                    "lat": item.zone_lat,
                    "lon": item.zone_lon,
                    "radius_m": item.zone_radius_meters or 100.0,
                },
                "target_lat": item.target_lat,
                "target_lon": item.target_lon,
                "radius_meters": item.radius_meters,
                "proximity_points": item.proximity_points,
                "proximity_mode": item.proximity_mode,
            })

        # All teams' latest positions.
        teams_result = await session.execute(select(Team))
        teams = []
        for team in teams_result.scalars().all():
            pos_result = await session.execute(
                select(TeamPosition).where(TeamPosition.team_id == team.id).order_by(TeamPosition.recorded_at.desc()).limit(1)
            )
            pos = pos_result.scalars().first()
            team_entry = {
                "team_id": team.id,
                "name": team.name,
                "position": None,
            }
            if pos and pos.recorded_at:
                # recorded_at is stored as naive UTC per model default.
                pos_dt = pos.recorded_at.replace(tzinfo=timezone.utc) if pos.recorded_at.tzinfo is None else pos.recorded_at
                age = (now - pos_dt).total_seconds()
                team_entry["position"] = {
                    "lat": pos.lat,
                    "lon": pos.lon,
                    "accuracy_m": pos.accuracy_m,
                    "age_seconds": int(age),
                    "stale": age > _STALE_THRESHOLD_SECONDS,
                }
            teams.append(team_entry)

        # All submissions with GPS (including unapproved).
        subs_result = await session.execute(
            select(Submission).where(
                Submission.gps_lat.isnot(None),
                Submission.gps_lon.isnot(None),
            ).order_by(Submission.submitted_at.desc())
        )
        submissions = []
        for sub in subs_result.scalars().all():
            age = 0
            if sub.submitted_at:
                sub_dt = sub.submitted_at.replace(tzinfo=timezone.utc) if sub.submitted_at.tzinfo is None else sub.submitted_at
                age = (now - sub_dt).total_seconds()
            submissions.append({
                "submission_id": sub.id,
                "item_id": sub.item_id,
                "team_id": sub.team_id,
                "lat": sub.gps_lat,
                "lon": sub.gps_lon,
                "status": sub.location_status,
                "verified": sub.verified,
                "age_seconds": int(age),
            })

    return {"items": items, "teams": teams, "submissions": submissions}


# --- Map Pages ---

@app.get("/map")
async def team_map_page(request: Request, _: None = Depends(require_team)):
    """Team-facing map page — HTML rendered with vendored Leaflet."""
    from .config import get_tile_url
    return templates.TemplateResponse(
        request, "map.html", _ctx(
            request,
            tile_url=get_tile_url(),
            map_api_url="/api/map",
            is_admin_view=False,
        )
    )


@app.get("/admin/map-view")
async def admin_map_page(request: Request, _: None = Depends(require_admin)):
    """Admin-facing map page — distinct path from the API route `/admin/map`."""
    from .config import get_tile_url
    return templates.TemplateResponse(
        request, "map.html", _ctx(
            request,
            tile_url=get_tile_url(),
            map_api_url="/admin/map",
            is_admin_view=True,
        )
    )


# --- Team Endpoints ---

async def _authenticate_team(request: Request, passphrase: str):
    """Shared team login: rate-limit, verify, set the session (§3.3).

    Used by both the form route ``/team/login`` and the JSON route
    ``/api/login`` so the two can never diverge (§2).
    """
    if _login_rate_limited(request):
        logger.warning(f"Login RATE-LIMITED for {_client_ip(request)}")
        raise HTTPException(status_code=429, detail="Too many login attempts. Please wait and try again.")
    async with async_session() as session:
        teams = (await session.execute(select(Team))).scalars().all()
    # Verify against every team so the check is constant-time per hash and does
    # not leak which team matched (§3.2).
    team = next((t for t in teams if verify_passphrase(passphrase, t.passphrase_hash)), None)
    if not team:
        raise HTTPException(status_code=401, detail="Invalid passphrase")
    request.session["team_id"] = team.id
    return team


@app.post("/team/login")
async def login(request: Request, passphrase: str = Form(...)):
    await _authenticate_team(request, passphrase)
    return {"message": "Logged in!", "redirect": "/hunt/dashboard"}


@app.post("/api/login")
async def api_login(request: Request):
    """JSON team login for native clients (§1). Reuses the form route's logic."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    passphrase = body.get("passphrase")
    if not isinstance(passphrase, str) or not passphrase:
        raise HTTPException(status_code=400, detail="passphrase is required")
    team = await _authenticate_team(request, passphrase)
    return {"team_id": team.id, "team_name": team.name}


async def _load_items_and_state(session, team_id):
    """Active hunt's items + this team's per-item state (§1).

    Shared by the HTML dashboard and ``GET /api/items`` so the two can never
    diverge (§2). ``item_state`` is keyed by item id with the same keys the
    dashboard template reads.
    """
    # Scope items to the active hunt so a multi-hunt install does not mix
    # item lists or inflate the maximum (§1.4).
    items_query = select(HuntItem)
    if _ACTIVE_HUNT_ID is not None:
        items_query = items_query.where(HuntItem.hunt_id == _ACTIVE_HUNT_ID)
    items = (await session.execute(items_query)).scalars().all()

    subs = (await session.execute(
        select(Submission).where(Submission.team_id == team_id)
    )).scalars().all()

    item_state = {}
    for sub in subs:
        state = item_state.setdefault(sub.item_id, {
            "attempts": 0, "locked": False, "points_awarded": 0, "has_bonus": False,
            "proximity_points_awarded": 0, "location_status": None,
        })
        state["attempts"] += 1
        if sub.verified:
            state["locked"] = True
            state["points_awarded"] = sub.points_awarded
            state["has_bonus"] = sub.ai_bonus_awarded > 0
            state["proximity_points_awarded"] = sub.proximity_points_awarded or 0
            state["location_status"] = sub.location_status
    return items, item_state


def _serialize_item(item, state) -> dict:
    """Team-safe item payload (§1).

    Deliberately omits ``target_lat``/``target_lon``/``radius_meters``: this is
    team-facing and leaking the true target lets a player walk straight to the
    spot (phase 2 §1). The display ``zone`` is allowed and mirrors ``/api/map``.
    """
    state = state or {}
    payload = {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "points": item.points,
        "bonus_points": item.bonus_points,
        "bonus_hint": item.bonus_hint,
        "locked": bool(state.get("locked", False)),
        "attempts": state.get("attempts", 0),
        "points_awarded": state.get("points_awarded", 0),
        "has_bonus": bool(state.get("has_bonus", False)),
        "proximity_points_awarded": state.get("proximity_points_awarded", 0),
        "location_status": state.get("location_status"),
        "max_attempts": MAX_ATTEMPTS_PER_ITEM,
        "zone": None,
    }
    if item.zone_lat is not None and item.zone_lon is not None:
        payload["zone"] = {
            "lat": item.zone_lat,
            "lon": item.zone_lon,
            "radius_m": item.zone_radius_meters or 100.0,
        }
    return payload


@app.get("/api/items")
async def api_items(request: Request, _: None = Depends(require_team)):
    """Active hunt's items with this team's state (§1).

    Team-facing: never includes the true target (see :func:`_serialize_item`).
    """
    team_id = request.session.get("team_id")
    async with async_session() as session:
        items, item_state = await _load_items_and_state(session, team_id)
    return [_serialize_item(item, item_state.get(item.id)) for item in items]


@app.get("/api/hunt")
async def api_hunt(request: Request, _: None = Depends(require_team)):
    """Hunt open/closed state for the team client.

    Returns exactly the dict :func:`get_hunt_state` produces, with ``ends_at``
    normalised to an ISO 8601 string (or ``None``) so it is JSON-serialisable.
    Team-authenticated and fail-closed, like the other ``/api/*`` routes.
    """
    state = dict(get_hunt_state())
    ends_at = state.get("ends_at")
    if isinstance(ends_at, datetime.datetime):
        state["ends_at"] = ends_at.isoformat()
    return state


@app.get("/hunt/dashboard")
async def hunt_dashboard(request: Request, _: None = Depends(require_team)):
    team_id = request.session.get("team_id")
    async with async_session() as session:
        items, item_state = await _load_items_and_state(session, team_id)

        total_points = sum(s["points_awarded"] for s in item_state.values() if s["locked"])
        total_proximity = sum(s["proximity_points_awarded"] for s in item_state.values() if s["locked"])

        def _item_max(item):
            # Proximity is only reachable with a target and mode != "off" (§2.4).
            proximity = 0
            if (item.proximity_mode or "bonus") != "off" and item.target_lat is not None and item.target_lon is not None:
                proximity = item.proximity_points or 0
            return item.points + item.bonus_points + proximity

        max_points = sum(_item_max(item) for item in items)
        progress = (total_points / max_points * 100) if max_points > 0 else 0

        return templates.TemplateResponse(
            request, "hunt_dashboard.html", _ctx(
                request,
                items=items,
                item_state=item_state,
                total_points=total_points,
                total_proximity=total_proximity,
                max_points=max_points,
                progress=progress,
                max_attempts=MAX_ATTEMPTS_PER_ITEM,
                team_id=team_id,
            )
        )


async def _load_leaderboard(session):
    """Verified points per team, highest first (§1).

    Shared by the HTML leaderboard and ``GET /api/leaderboard``.
    """
    rank_result = await session.execute(
        select(Team.name, func.sum(Submission.points_awarded))
        .join(Submission, Team.id == Submission.team_id)
        .where(Submission.verified == True)
        .group_by(Team.name)
        .order_by(func.sum(Submission.points_awarded).desc())
    )
    return rank_result.all()


@app.get("/api/leaderboard")
async def api_leaderboard(request: Request, _: None = Depends(require_team)):
    """JSON leaderboard (§1). Team-authenticated — it must not be public."""
    async with async_session() as session:
        rows = await _load_leaderboard(session)
    return [{"team_name": name, "points": int(points or 0)} for name, points in rows]


@app.get("/leaderboard")
async def get_leaderboard(request: Request, _: None = Depends(require_team)):
    async with async_session() as session:
        leaderboard = await _load_leaderboard(session)

        gallery_entries = []
        hunt_state = get_hunt_state()
        if hunt_state["is_open"] == False and hunt_state["ends_at"] is not None:
            sub_result = await session.execute(
                select(Submission, HuntItem.name, HuntItem.points, HuntItem.bonus_hint, Team.name)
                .join(HuntItem, Submission.item_id == HuntItem.id)
                .join(Team, Submission.team_id == Team.id)
                .where(Submission.verified == True)
                .order_by(Team.name.asc(), Submission.submitted_at.asc())
            )
            for submission, item_name, item_points, bonus_hint, team_name in sub_result.all():
                review = {}
                if submission.ai_review_result:
                    try:
                        review = json.loads(submission.ai_review_result)
                    except (TypeError, ValueError):
                        review = {}
                base = _thumb_base(submission)
                gallery_entries.append({
                    "submission": submission,
                    "item_name": item_name,
                    "item_points": item_points,
                    "bonus_hint": bonus_hint,
                    "team_name": team_name,
                    "review": review,
                    "thumb_url": f"/uploads/thumb/{base}.jpg",
                    "display_url": f"/uploads/display/{base}.jpg",
                })

        return templates.TemplateResponse(
            request, "leaderboard.html", _ctx(
                request,
                leaderboard=leaderboard,
                gallery_entries=gallery_entries,
            )
        )


async def _load_gallery_entries(session, team_id):
    """This team's submissions, newest first, with resolved photo URLs (§1).

    Shared by the HTML gallery and ``GET /api/gallery``.
    """
    result = await session.execute(
        select(Submission, HuntItem.name, HuntItem.points, HuntItem.bonus_hint)
        .join(HuntItem, Submission.item_id == HuntItem.id)
        .where(Submission.team_id == team_id)
        .order_by(Submission.submitted_at.desc())
    )
    rows = result.all()

    entries = []
    for submission, item_name, item_points, bonus_hint in rows:
        base = _thumb_base(submission)
        entries.append({
            "submission": submission,
            "item_name": item_name,
            "item_points": item_points,
            "bonus_hint": bonus_hint,
            "thumb_url": f"/uploads/thumb/{base}.jpg",
            "display_url": f"/uploads/display/{base}.jpg",
        })
    return entries


@app.get("/api/gallery")
async def api_gallery(request: Request, _: None = Depends(require_team)):
    """This team's own submissions only (§1)."""
    team_id = request.session.get("team_id")
    async with async_session() as session:
        entries = await _load_gallery_entries(session, team_id)
    return [
        {
            "item_name": e["item_name"],
            "thumb_url": e["thumb_url"],
            "display_url": e["display_url"],
            "verified": bool(e["submission"].verified),
            "points_awarded": e["submission"].points_awarded or 0,
            "proximity_points_awarded": e["submission"].proximity_points_awarded or 0,
            "location_status": e["submission"].location_status,
            "attempt_number": e["submission"].attempt_number,
            "submitted_at": e["submission"].submitted_at.isoformat() if e["submission"].submitted_at else None,
        }
        for e in entries
    ]


@app.get("/gallery")
async def gallery(request: Request, _: None = Depends(require_team)):
    team_id = request.session.get("team_id")
    async with async_session() as session:
        entries = await _load_gallery_entries(session, team_id)

    return templates.TemplateResponse(
        request, "gallery.html", _ctx(request, entries=entries)
    )


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


@app.get("/admin/dashboard", dependencies=[Depends(require_admin)])
async def admin_dashboard(request: Request):
    return templates.TemplateResponse(request, "admin_dashboard.html", _ctx(request))


@app.post("/admin/hunt/start", dependencies=[Depends(require_admin)])
async def admin_hunt_start():
    state = get_hunt_state()
    duration = state["duration_minutes"]
    ends_at = datetime.datetime.now(timezone.utc) + timedelta(minutes=duration)
    set_hunt_ends_at(ends_at.isoformat())
    return {"message": f"Hunt started — ends in {duration} minutes."}


@app.post("/admin/hunt/extend", dependencies=[Depends(require_admin)])
async def admin_hunt_extend():
    state = get_hunt_state()
    if not state["ends_at"]:
        return {"message": "Hunt is not running."}
    new_ends_at = state["ends_at"] + timedelta(minutes=5)
    set_hunt_ends_at(new_ends_at.isoformat())
    return {"message": "Hunt extended by 5 minutes."}


@app.post("/admin/hunt/stop", dependencies=[Depends(require_admin)])
async def admin_hunt_stop():
    set_hunt_ends_at("")
    return {"message": "Hunt stopped."}


@app.get("/admin/all-gallery", dependencies=[Depends(require_admin)])
async def admin_all_gallery(request: Request):
    async with async_session() as session:
        result = await session.execute(
            select(Submission, HuntItem.name, HuntItem.points, HuntItem.bonus_hint, Team.name)
            .join(HuntItem, Submission.item_id == HuntItem.id)
            .join(Team, Submission.team_id == Team.id)
            .order_by(Team.name.asc(), Submission.submitted_at.desc())
        )
        rows = result.all()

    entries = []
    for submission, item_name, item_points, bonus_hint, team_name in rows:
        review = {}
        if submission.ai_review_result:
            try:
                review = json.loads(submission.ai_review_result)
            except (TypeError, ValueError):
                review = {}
        base = _thumb_base(submission)
        entries.append({
            "submission": submission,
            "item_name": item_name,
            "item_points": item_points,
            "bonus_hint": bonus_hint,
            "team_name": team_name,
            "review": review,
            "thumb_url": f"/uploads/thumb/{base}.jpg",
            "display_url": f"/uploads/display/{base}.jpg",
        })

    return templates.TemplateResponse(
        request, "admin_all_gallery.html", _ctx(request, entries=entries)
    )


@app.get("/admin/hints", dependencies=[Depends(require_admin)])
async def admin_hints(request: Request):
    async with async_session() as session:
        result = await session.execute(
            select(HuntItem, Hunt.name)
            .join(Hunt, HuntItem.hunt_id == Hunt.id)
            .order_by(Hunt.name.asc(), HuntItem.id.asc())
        )
        rows = result.all()
    items_with_hunts = []
    for item, hunt_name in rows:
        try:
            required = json.loads(item.required_properties or "[]")
        except (TypeError, ValueError):
            required = []
        try:
            bonus = json.loads(item.bonus_properties or "[]")
        except (TypeError, ValueError):
            bonus = []
        items_with_hunts.append((item, hunt_name, required, bonus))
    return templates.TemplateResponse(
        request, "admin_hints.html", _ctx(request, items_with_hunts=items_with_hunts)
    )


@app.get("/result")
async def result_page(request: Request, status: str, message: str):
    return templates.TemplateResponse(
        request, "result.html", _ctx(request, status=status, message=message)
    )


# --- Submission Logic ---

def _parse_exif_offset(offset_str):
    """Parse an EXIF ``±HH:MM`` offset string into a timedelta, or ``None``."""
    if isinstance(offset_str, bytes):
        offset_str = offset_str.decode("utf-8", errors="ignore")
    if not offset_str or not isinstance(offset_str, str):
        return None
    text = offset_str.strip()
    if not text or text[0] not in "+-":
        return None
    sign = 1 if text[0] == "+" else -1
    rest = text[1:]
    try:
        if ":" in rest:
            hours_str, minutes_str = rest.split(":", 1)
            hours, minutes = int(hours_str), int(minutes_str)
        elif len(rest) >= 4:
            hours, minutes = int(rest[:2]), int(rest[2:4])
        elif len(rest) == 2:
            hours, minutes = int(rest), 0
        else:
            return None
    except (ValueError, IndexError):
        return None
    return timedelta(hours=hours, minutes=minutes) * sign


def get_photo_taken_at(img: Image.Image) -> datetime.datetime | None:
    """Extract the capture time in priority order.

    ``DateTimeOriginal`` (36867) -> ``DateTimeDigitized`` (36868) -> IFD0
    ``DateTime`` (306). The first two live in the **Exif sub-IFD**, not IFD0 —
    Pillow's ``Exif.get()`` only consults IFD0, so they must be read via
    ``get_ifd(ExifTags.IFD.Exif)``. Each tag's **own** offset is applied:
    ``OffsetTimeOriginal`` (0x9010) only to DateTimeOriginal and
    ``OffsetTimeDigitized`` (0x9011) only to DateTimeDigitized. A bare IFD0
    ``DateTime`` has no offset tag.

    Returns a timezone-aware UTC datetime when an offset applies, otherwise a
    naive datetime (treated as server local time by the caller). Returns
    ``None`` only when no timestamp tag exists at all.
    """
    from PIL import ExifTags
    try:
        exif = img.getexif()
    except Exception:
        return None
    if not exif:
        return None

    try:
        sub_ifd = exif.get_ifd(ExifTags.IFD.Exif) or {}
    except Exception:
        sub_ifd = {}

    candidates = (
        (sub_ifd.get(36867), sub_ifd.get(0x9010)),   # DateTimeOriginal + its offset
        (sub_ifd.get(36868), sub_ifd.get(0x9011)),   # DateTimeDigitized + its offset
        (exif.get(306), None),                        # IFD0 DateTime, no offset
    )
    for raw_value, offset_raw in candidates:
        if not raw_value:
            continue
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8", errors="ignore")
        try:
            taken_at = datetime.datetime.strptime(str(raw_value), "%Y:%m:%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        offset = _parse_exif_offset(offset_raw)
        if offset is not None:
            return (taken_at - offset).replace(tzinfo=timezone.utc)
        return taken_at
    return None


def make_thumbnails(original_path: str, base_name: str) -> None:
    """Generate thumb (200px) + display (1200px) JPEGs into the standard dirs.

    EXIF orientation is applied first so phone photos are not rendered
    sideways (§1.2). Derived images have no EXIF, which is intentional.
    """
    thumb_out  = os.path.join(THUMB_DIR,  f"{base_name}.jpg")
    display_out = os.path.join(DISPLAY_DIR, f"{base_name}.jpg")

    with Image.open(original_path) as img:
        img = ImageOps.exif_transpose(img) or img
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        t = img.copy()
        t.thumbnail((200, 200), Image.LANCZOS)
        t.save(thumb_out, "JPEG", quality=85, optimize=True)

        d = img.copy()
        d.thumbnail((1200, 1200), Image.LANCZOS)
        d.save(display_out, "JPEG", quality=90, optimize=True)


def _submission_result(*, verified, points_awarded, proximity_points_awarded,
                       location_status, message, redirect) -> dict:
    """Uniform result for the submission pipeline, shared by both routes (§2)."""
    return {
        "verified": verified,
        "points_awarded": points_awarded,
        "proximity_points_awarded": proximity_points_awarded,
        "location_status": location_status,
        "message": message,
        "redirect": redirect,
    }


def _submission_failure(message, redirect, location_status: str = "unknown") -> dict:
    """A handled rejection that never reached scoring.

    ``location_status`` defaults to ``"unknown"`` because no coordinate verdict
    was computed; paths that do score pass the real status explicitly.
    """
    return _submission_result(
        verified=False,
        points_awarded=0,
        proximity_points_awarded=0,
        location_status=location_status,
        message=message,
        redirect=redirect,
    )


async def _process_submission(*, item_id, team_id, session_team_id, file, live_lat, live_lon) -> dict:
    """The single submission pipeline (§2).

    Both the form route ``/submit`` and the JSON route ``/api/submit`` call
    this, so AI review, EXIF parsing, proximity scoring and the attempt/lock
    rules cannot diverge between them. Returns a :func:`_submission_result`.
    """
    if session_team_id != team_id:
        logger.warning(f"Submission REJECTED: posted team_id {team_id} does not match logged-in team {session_team_id}")
        return _submission_failure("Team mismatch", "/result?status=error&message=You are not logged in as that team.")

    if not get_hunt_state()["is_open"]:
        logger.info(f"Submission REJECTED: Hunt is not currently open (team_id={team_id}, item_id={item_id})")
        return _submission_failure(
            "Hunt closed",
            "/result?status=error&message=The hunt is not currently open. Please wait for the admin to start it.",
        )

    original_path = thumb_path = display_path = None

    try:
        async with async_session() as session:
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar()
            team_name = team.name if team else f"Unknown Team (ID: {team_id})"
            item = (await session.execute(select(HuntItem).where(HuntItem.id == item_id))).scalar()

        if not item:
            logger.error(f"Submission REJECTED: Team '{team_name}' submitted for non-existent Item ID {item_id}")
            return _submission_failure("Item not found", "/result?status=error&message=Invalid item.")
        item_name = item.name

        contents = await file.read()
        if not contents:
            logger.warning(f"Submission REJECTED: Empty file from Team '{team_name}'")
            return _submission_failure("Empty file", "/result?status=error&message=The uploaded file is empty.")

        filename = build_original_filename(file.filename)
        original_path = os.path.join(ORIGINALS_DIR, filename)
        base_name = os.path.splitext(filename)[0]
        thumb_path = os.path.join(THUMB_DIR, f"{base_name}.jpg")
        display_path = os.path.join(DISPLAY_DIR, f"{base_name}.jpg")

        with open(original_path, "wb") as buffer:
            buffer.write(contents)

        make_thumbnails(original_path, base_name)

        max_age_hours = get_max_photo_age_hours()
        try:
            with Image.open(original_path) as _img:
                taken_at = get_photo_taken_at(_img)
                coords = parse_gps(_img)
        except Exception:
            taken_at = None
            coords = None

        if taken_at is None:
            _cleanup_files(original_path, thumb_path, display_path)
            logger.info(f"Submission REJECTED (No EXIF timestamp): Team '{team_name}' — '{item_name}'")
            return _submission_failure("No timestamp", "/result?status=error&message=Photo has no EXIF timestamp. Ensure your camera's date/time is enabled.")

        max_age = timedelta(hours=max_age_hours)
        if taken_at.tzinfo is not None:
            now_ref = datetime.datetime.now(timezone.utc)
            tz_label = "UTC"
        else:
            now_ref = datetime.datetime.now()
            tz_label = "local"

        age = now_ref - taken_at

        if age > max_age:
            hours = int(age.total_seconds() // 3600)
            minutes = int((age.total_seconds() % 3600) // 60)
            _cleanup_files(original_path, thumb_path, display_path)
            logger.info(f"Submission REJECTED (Photo too old): Team '{team_name}' — '{item_name}' "
                        f"taken at {taken_at} ({tz_label}), age {hours}h {minutes}m, max allowed {max_age_hours}h")
            return _submission_failure("Photo too old", f"/result?status=error&message=Photo is {hours}h {minutes}m old. Photos must be less than {max_age_hours:g} hours old.")

        if age < timedelta(hours=-1):
            logger.warning(f"Photo timestamp is in the future: Team '{team_name}' — '{item_name}' "
                           f"taken at {taken_at} ({tz_label}), current {now_ref}, age {age}")

        async with async_session() as session:
            locked = (await session.execute(
                select(Submission).where(
                    Submission.team_id == team_id,
                    Submission.item_id == item_id,
                    Submission.verified == True
                )
            )).scalar_one_or_none()
            if locked:
                _cleanup_files(original_path, thumb_path, display_path)
                logger.info(f"Submission REJECTED (Locked): Team '{team_name}' — '{item_name}' is already locked")
                return _submission_failure("Item locked", "/result?status=error&message=Your team already has an approved submission for this item.")

            attempt_count = (await session.execute(
                select(func.count(Submission.id)).where(
                    Submission.team_id == team_id,
                    Submission.item_id == item_id
                )
            )).scalar()
            if attempt_count >= MAX_ATTEMPTS_PER_ITEM:
                _cleanup_files(original_path, thumb_path, display_path)
                logger.info(f"Submission REJECTED (Max Attempts): Team '{team_name}' used all {MAX_ATTEMPTS_PER_ITEM} attempts on '{item_name}'")
                return _submission_failure("Max attempts", f"/result?status=error&message=Maximum {MAX_ATTEMPTS_PER_ITEM} attempts reached for this item.")

        required = json.loads(item.required_properties or "[]")
        bonus = json.loads(item.bonus_properties or "[]")
        review = await run_in_threadpool(
            review_image, original_path, item.name, item.description or "", required, bonus
        )

        all_required_met = (
            review["is_target"]
            and review["confidence"] >= CONFIDENCE_THRESHOLD
            and len(review["missed_required"]) == 0
        )
        all_bonus_met = all_required_met and len(review["missed_bonus"]) == 0 and len(bonus) > 0
        awarded_bonus = item.bonus_points if all_bonus_met else 0
        ai_points = item.points + awarded_bonus if all_required_met else 0

        # §2.3 — proximity is added *beside* the AI points, never instead of them.
        # It is a bonus on a valid find, so it is awarded only when the photo is
        # approved (all required met). location_status and distance_m are always
        # persisted for diagnostics, even on a rejected photo.
        location_status, proximity_points, distance_m = score_proximity(item, coords)
        proximity_points_awarded = proximity_points if all_required_met else 0
        points_awarded = ai_points + proximity_points_awarded

        is_locked = all_required_met
        if (item.proximity_mode or "bonus") == "gate":
            # gate mode: proximity also gates approval.
            is_locked = all_required_met and location_status in ("hit", "near")

        # §2.4 — a missing GPS is expected, never silent.
        if location_status == "missing":
            logger.info(f"Submission LOCATION UNAVAILABLE (no GPS in photo): Team '{team_name}' — "
                        f"'{item_name}'. AI points {ai_points}, proximity points 0")
        elif distance_m is not None:
            logger.info(f"Submission LOCATION {location_status.upper()}: Team '{team_name}' — '{item_name}' "
                        f"distance {distance_m:.1f}m, proximity points {proximity_points_awarded}")

        # §2.5 — client-supplied live fix is untrusted display data. Stored only,
        # never scored from, and never overrides the EXIF-derived value.
        live_lat_value = _opt_float(live_lat)
        live_lon_value = _opt_float(live_lon)
        if live_lat_value is not None and not (-90.0 <= live_lat_value <= 90.0):
            live_lat_value = None
        if live_lon_value is not None and not (-180.0 <= live_lon_value <= 180.0):
            live_lon_value = None

        async with async_session() as session:
            # Re-check right before insert: the AI call above opened a race window.
            locked = (await session.execute(
                select(Submission).where(
                    Submission.team_id == team_id,
                    Submission.item_id == item_id,
                    Submission.verified == True
                )
            )).scalar_one_or_none()
            attempt_count = (await session.execute(
                select(func.count(Submission.id)).where(
                    Submission.team_id == team_id,
                    Submission.item_id == item_id
                )
            )).scalar()
            if locked or attempt_count >= MAX_ATTEMPTS_PER_ITEM:
                await session.rollback()
                _cleanup_files(original_path, thumb_path, display_path)
                logger.info(f"Submission REJECTED (Race): Team '{team_name}' — '{item_name}' became locked or maxed during AI review")
                return _submission_failure("Item locked", "/result?status=error&message=That item is no longer available for submission.")

            attempt_number = attempt_count + 1
            sub = Submission(
                item_id=item_id, team_id=team_id,
                photo_path=original_path,
                thumbnail_path=thumb_path,
                display_path=display_path,
                verified=is_locked,
                ai_review_result=json.dumps(review),
                ai_bonus_awarded=awarded_bonus,
                points_awarded=points_awarded,
                attempt_number=attempt_number,
                captured_at=taken_at,
                gps_lat=coords[0] if coords else None,
                gps_lon=coords[1] if coords else None,
                distance_m=distance_m,
                location_status=location_status,
                proximity_points_awarded=proximity_points_awarded,
                live_lat=live_lat_value,
                live_lon=live_lon_value,
            )
            session.add(sub)
            await session.commit()

        location_note = _location_note(location_status, proximity_points_awarded)
        if is_locked:
            bonus_text = f" (+{awarded_bonus} bonus for matching all bonus properties!)" if awarded_bonus > 0 else ""
            logger.info(f"Submission APPROVED: Team '{team_name}' verified '{item_name}' on attempt {attempt_number}/{MAX_ATTEMPTS_PER_ITEM}. "
                        f"Points: {points_awarded} (AI {ai_points} + proximity {proximity_points_awarded}), "
                        f"location {location_status}. Reason: {review['reason']}")
            message = f"Verified! You earned {points_awarded} points{bonus_text}.{location_note}"
            return _submission_result(
                verified=True,
                points_awarded=points_awarded,
                proximity_points_awarded=proximity_points_awarded,
                location_status=location_status,
                message=message,
                redirect=f"/result?status=success&message={message}",
            )

        logger.info(f"Submission REJECTED: Team '{team_name}' — '{item_name}' attempt {attempt_number}/{MAX_ATTEMPTS_PER_ITEM}. "
                    f"Reason: {review['reason']} | Confidence: {review['confidence']} | location {location_status}")
        reason = review.get("reason", "The photo doesn't appear to match the target item.")
        if all_required_met and (item.proximity_mode or "bonus") == "gate":
            reason = "You must be within the item's area to earn this one."
        # §2.4: a missing GPS is stated in words even on rejection. Proximity
        # points are not advertised here — the submission was not approved.
        if location_status == "missing":
            reason = f"{reason} Location unavailable — no proximity points."
        message = f"Not approved: {reason}"
        return _submission_result(
            verified=False,
            points_awarded=points_awarded,
            proximity_points_awarded=proximity_points_awarded,
            location_status=location_status,
            message=message,
            redirect=f"/result?status=error&message={message} (Attempt {attempt_number}/{MAX_ATTEMPTS_PER_ITEM})",
        )

    except Image.UnidentifiedImageError:
        logger.warning(f"Submission REJECTED (Unsupported Format): {file.filename if file else '?'}")
        _cleanup_files(original_path, thumb_path, display_path)
        return _submission_failure("Unsupported image format.", "/result?status=error&message=The file format is not supported.")

    except Exception:
        logger.exception("Submission REJECTED (Server Error)")
        _cleanup_files(original_path, thumb_path, display_path)
        return _submission_failure("Server error", "/result?status=error&message=An unexpected error occurred.")


@app.post("/submit")
async def submit_photo(request: Request, item_id: int = Form(...), team_id: int = Form(...), file: UploadFile = File(...), live_lat: str | None = Form(None), live_lon: str | None = Form(None), _: None = Depends(require_team)):
    """HTML submission route — unchanged contract (message + redirect)."""
    result = await _process_submission(
        item_id=item_id,
        team_id=team_id,
        session_team_id=request.session.get("team_id"),
        file=file,
        live_lat=live_lat,
        live_lon=live_lon,
    )
    return {"message": result["message"], "redirect": result["redirect"]}


@app.post("/api/submit")
async def api_submit(request: Request, item_id: int = Form(...), file: UploadFile = File(...), live_lat: str | None = Form(None), live_lon: str | None = Form(None), _: None = Depends(require_team)):
    """JSON submission route (§1) — same pipeline, clean JSON contract.

    The team is taken from the session, so no ``team_id`` field is needed.
    Handled rejections still return HTTP 200 with ``verified=False`` and the
    reason in ``message``; auth failures return 403 and malformed requests 4xx.
    """
    session_team_id = request.session.get("team_id")
    result = await _process_submission(
        item_id=item_id,
        team_id=session_team_id,
        session_team_id=session_team_id,
        file=file,
        live_lat=live_lat,
        live_lon=live_lon,
    )
    return {
        "verified": result["verified"],
        "points_awarded": result["points_awarded"],
        "proximity_points_awarded": result["proximity_points_awarded"],
        "location_status": result["location_status"],
        "message": result["message"],
    }