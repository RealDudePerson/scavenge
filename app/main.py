from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request, status, responses
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from sqlalchemy.future import select
from sqlalchemy import delete, func
from .database import async_session, init_db
from .models import Hunt, HuntItem, Team, Submission, Base
from .config import HUNTS_DIR, TEAMS_DIR, UPLOADS_DIR, ORIGINALS_DIR, THUMB_DIR, DISPLAY_DIR, get_admin_password, get_max_photo_age_hours, get_hunt_state, set_hunt_ends_at
from .ai_review import review_image
import logging
import json
import yaml
import os
import uuid
import datetime
from datetime import timezone, timedelta
from PIL import Image, ExifTags
import pillow_heif

pillow_heif.register_heif_opener()

logger = logging.getLogger("uvicorn.error")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET", uuid.uuid4().hex))
templates = Jinja2Templates(directory="templates")
app.mount("/uploads/originals", StaticFiles(directory="uploads/originals"), name="uploads-originals")
app.mount("/uploads/thumb", StaticFiles(directory="uploads/thumb"), name="uploads-thumb")
app.mount("/uploads/display", StaticFiles(directory="uploads/display"), name="uploads-display")

@app.on_event("startup")
async def startup():
    await init_db()

MAX_ATTEMPTS_PER_ITEM = 10
CONFIDENCE_THRESHOLD = 0.7

def require_admin(request: Request):
    if request.session.get("is_admin") != True:
        return responses.RedirectResponse(url="/", status_code=303)
    return True

def require_team(request: Request):
    if not request.session.get("team_id"):
        return responses.RedirectResponse(url="/", status_code=303)
    return True

def _cleanup_files(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

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

async def get_active_theme() -> dict:
    """Return the first hunt's theme, or DEFAULT_THEME if no hunts exist."""
    try:
        async with async_session() as session:
            result = await session.execute(select(Hunt).order_by(Hunt.id.asc()).limit(1))
            hunt = result.scalar()
            if not hunt:
                return DEFAULT_THEME.copy()
            return {
                "primary":      hunt.theme_primary      or DEFAULT_THEME["primary"],
                "secondary":    hunt.theme_secondary    or DEFAULT_THEME["secondary"],
                "accent":       hunt.theme_accent       or DEFAULT_THEME["accent"],
                "background":   hunt.theme_background   or DEFAULT_THEME["background"],
                "surface":      hunt.theme_surface      or DEFAULT_THEME["surface"],
                "text":         hunt.theme_text         or DEFAULT_THEME["text"],
                "text_muted":   hunt.theme_text_muted   or DEFAULT_THEME["text_muted"],
                "success":      hunt.theme_success      or DEFAULT_THEME["success"],
                "warning":      hunt.theme_warning      or DEFAULT_THEME["warning"],
                "info":         hunt.theme_info         or DEFAULT_THEME["info"],
            }
    except Exception:
        return DEFAULT_THEME.copy()

# --- Auth Endpoints ---

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return responses.RedirectResponse(url="/", status_code=303)

@app.get("/admin/login")
async def admin_login_page(request: Request):
    is_admin = request.session.get("is_admin") == True
    is_team = request.session.get("team_id") is not None
    return templates.TemplateResponse(
        request=request, name="admin_login.html",
        context={"is_admin": is_admin, "is_team_member": is_team, "theme": await get_active_theme()}
    )

@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if password.strip() == get_admin_password():
        request.session["is_admin"] = True
        return {"message": "Login successful", "redirect": "/admin/dashboard"}
    raise HTTPException(status_code=401, detail="Invalid password")

@app.post("/admin/reset")
async def reset_db(request: Request, _: bool = Depends(require_admin)):
    from .database import engine
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        # Wipe all upload subdirectories (originals, thumb, display)
        for upload_dir in (ORIGINALS_DIR, THUMB_DIR, DISPLAY_DIR):
            if os.path.isdir(upload_dir):
                for filename in os.listdir(upload_dir):
                    file_path = os.path.join(upload_dir, filename)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
        # Also wipe any orphan files directly in uploads/ root
        # (from the old flat-directory scheme before subdirs were added)
        if os.path.isdir(UPLOADS_DIR):
            for filename in os.listdir(UPLOADS_DIR):
                file_path = os.path.join(UPLOADS_DIR, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        return {"message": "All progress and teams reset."}
    except Exception as e:
        logger.exception("Reset failed")
        return {"message": f"Reset failed: {str(e)}"}, 500

@app.post("/admin/cleanup-orphans", dependencies=[Depends(require_admin)])
async def cleanup_orphans():
    """Delete upload files that aren't referenced in any Submission row."""
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
                description=data.get('description', ''),
                theme_primary=theme.get('primary', '#FF6B9D'),
                theme_secondary=theme.get('secondary', '#FEC868'),
                theme_accent=theme.get('accent', '#7AC74F'),
                theme_background=theme.get('background', '#FFF8F0'),
                theme_surface=theme.get('surface', '#FFFFFF'),
                theme_text=theme.get('text', '#2D3142'),
                theme_text_muted=theme.get('text_muted', '#6C757D'),
                theme_success=theme.get('success', '#06A77D'),
                theme_warning=theme.get('warning', '#E63946'),
                theme_info=theme.get('info', '#4A90E2'),
            )
            session.add(hunt)
            await session.commit()
            for item_data in data['items']:
                required = item_data.get('required_properties')
                if not required or not isinstance(required, list) or len(required) == 0:
                    logger.warning(f"Skipping item '{item_data.get('name', '?')}' in '{data['name']}': missing required_properties")
                    skipped.append(item_data.get('name', '?'))
                    continue
                item = HuntItem(
                    hunt_id=hunt.id,
                    name=item_data['name'],
                    description=item_data.get('description', ''),
                    points=item_data.get('points', 0),
                    bonus_points=item_data.get('bonus_points', 0),
                    required_properties=json.dumps(required),
                    bonus_properties=json.dumps(item_data.get('bonus_properties', [])),
                    bonus_hint=item_data.get('bonus_hint'),
                )
                session.add(item)
            await session.commit()
            loaded.append(data['name'])
    return {"message": f"Hunts loaded. {len(loaded)} hunt(s), skipped {len(skipped)} item(s) without required_properties."}

@app.post("/admin/load-teams", dependencies=[Depends(require_admin)])
async def load_teams():
    with open(os.path.join(TEAMS_DIR, "teams.yaml"), "r") as f:
        data = yaml.safe_load(f)
    async with async_session() as session:
        for team_data in data['teams']:
            result = await session.execute(select(Team).where(Team.passphrase == team_data['passphrase']))
            if result.scalar():
                continue
            team = Team(name=team_data['name'], description=team_data.get('description', ''), passphrase=team_data['passphrase'])
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
    review = review_image(sub.photo_path, item.name, item.description or "", required, bonus)

    all_required_met = (
        review["is_target"]
        and review["confidence"] >= CONFIDENCE_THRESHOLD
        and len(review["missed_required"]) == 0
    )
    all_bonus_met = all_required_met and len(review["missed_bonus"]) == 0 and len(bonus) > 0
    awarded_bonus = item.bonus_points if all_bonus_met else 0
    points_awarded = item.points + awarded_bonus if all_required_met else 0

    async with async_session() as session:
        sub = (await session.execute(select(Submission).where(Submission.id == sub_id))).scalar_one()
        sub.ai_review_result = json.dumps(review)
        sub.ai_bonus_awarded = awarded_bonus
        sub.points_awarded = points_awarded
        sub.verified = all_required_met
        await session.commit()

    return {
        "verified": all_required_met,
        "bonus_awarded": awarded_bonus,
        "points_awarded": points_awarded,
        "review": review,
    }

# --- Team Endpoints ---

@app.post("/team/login")
async def login(request: Request, passphrase: str = Form(...)):
    async with async_session() as session:
        result = await session.execute(select(Team).where(Team.passphrase == passphrase))
        team = result.scalar()
        if not team:
            raise HTTPException(status_code=401, detail="Invalid passphrase")
        request.session["team_id"] = team.id
        return {"message": "Logged in!", "redirect": "/hunt/dashboard"}

@app.get("/hunt/dashboard")
async def hunt_dashboard(request: Request, _: bool = Depends(require_team)):
    team_id = request.session.get("team_id")
    async with async_session() as session:
        items_result = await session.execute(select(HuntItem))
        items = items_result.scalars().all()

        subs_result = await session.execute(
            select(Submission).where(Submission.team_id == team_id)
        )
        subs = subs_result.scalars().all()

        # Map item_id -> (attempts, locked, points_awarded, has_bonus)
        item_state = {}
        for sub in subs:
            state = item_state.setdefault(sub.item_id, {
                "attempts": 0, "locked": False, "points_awarded": 0, "has_bonus": False
            })
            state["attempts"] += 1
            if sub.verified:
                state["locked"] = True
                state["points_awarded"] = sub.points_awarded
                state["has_bonus"] = sub.ai_bonus_awarded > 0

        total_points = sum(s["points_awarded"] for s in item_state.values() if s["locked"])
        max_points = sum(item.points + item.bonus_points for item in items)
        progress = (total_points / max_points * 100) if max_points > 0 else 0

        return templates.TemplateResponse(
            request=request,
            name="hunt_dashboard.html",
            context={
                "items": items,
                "item_state": item_state,
                "total_points": total_points,
                "progress": progress,
                "max_attempts": MAX_ATTEMPTS_PER_ITEM,
                "session": request.session,
                "is_admin": False,
                "is_team_member": True,
                "theme": await get_active_theme(),
                "hunt_state": get_hunt_state(),
            }
        )

@app.get("/leaderboard")
async def get_leaderboard(request: Request):
    is_admin = request.session.get("is_admin") == True
    is_team = request.session.get("team_id") is not None
    async with async_session() as session:
        # Ranked teams (always shown)
        rank_result = await session.execute(
            select(Team.name, func.sum(Submission.points_awarded))
            .join(Submission, Team.id == Submission.team_id)
            .where(Submission.verified == True)
            .group_by(Team.name)
            .order_by(func.sum(Submission.points_awarded).desc())
        )
        leaderboard = rank_result.all()

        # All approved submissions with team + item + AI review details
        # Only fetched when the hunt has actually expired — keeps the
        # non-expired leaderboard fast.
        gallery_entries = []
        hunt_state = get_hunt_state()
        if hunt_state["is_expired"]:
            sub_result = await session.execute(
                select(Submission, HuntItem.name, HuntItem.points, HuntItem.bonus_hint, Team.name)
                .join(HuntItem, Submission.item_id == HuntItem.id)
                .join(Team, Submission.team_id == Team.id)
                .where(Submission.verified == True)
                .order_by(Team.name.asc(), Submission.submitted_at.asc())
            )
            for submission, item_name, item_points, bonus_hint, team_name in sub_result.all():
                # Parse the stored AI review JSON (defensive — could be NULL/legacy)
                review = {}
                if submission.ai_review_result:
                    try:
                        review = json.loads(submission.ai_review_result)
                    except (TypeError, ValueError):
                        review = {}
                base = os.path.splitext(os.path.basename(submission.thumbnail_path or ""))[0]
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
            request=request, name="leaderboard.html",
            context={
                "leaderboard": leaderboard,
                "gallery_entries": gallery_entries,
                "is_admin": is_admin,
                "is_team_member": is_team,
                "theme": await get_active_theme(),
                "hunt_state": hunt_state,
            }
        )

@app.get("/gallery")
async def gallery(request: Request, _: bool = Depends(require_team)):
    team_id = request.session.get("team_id")
    async with async_session() as session:
        result = await session.execute(
            select(Submission, HuntItem.name, HuntItem.points, HuntItem.bonus_hint)
            .join(HuntItem, Submission.item_id == HuntItem.id)
            .where(Submission.team_id == team_id)
            .order_by(Submission.submitted_at.desc())
        )
        rows = result.all()

    # Pre-build URLs server-side so the template doesn't have to manipulate paths
    entries = []
    for submission, item_name, item_points, bonus_hint in rows:
        base = os.path.splitext(os.path.basename(submission.thumbnail_path or ""))[0]
        entries.append({
            "submission": submission,
            "item_name": item_name,
            "item_points": item_points,
            "bonus_hint": bonus_hint,
            "thumb_url": f"/uploads/thumb/{base}.jpg",
            "display_url": f"/uploads/display/{base}.jpg",
        })

    return templates.TemplateResponse(
        request=request, name="gallery.html",
        context={
            "entries": entries,
            "is_admin": False,
            "is_team_member": True,
            "theme": await get_active_theme(),
            "hunt_state": get_hunt_state(),
        }
    )

@app.get("/")
async def index(request: Request):
    is_admin = request.session.get("is_admin") == True
    is_team = request.session.get("team_id") is not None
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"is_admin": is_admin, "is_team_member": is_team, "theme": await get_active_theme()}
    )

@app.get("/admin/dashboard")
async def admin_dashboard(request: Request, _: bool = Depends(require_admin)):
    is_admin = request.session.get("is_admin") == True
    is_team = request.session.get("team_id") is not None
    return templates.TemplateResponse(
        request=request, name="admin_dashboard.html",
        context={
            "is_admin": is_admin,
            "is_team_member": is_team,
            "theme": await get_active_theme(),
            "hunt_state": get_hunt_state(),
        }
    )

@app.post("/admin/hunt/start")
async def admin_hunt_start(_: bool = Depends(require_admin)):
    """Start (or restart) the hunt timer with the configured duration."""
    state = get_hunt_state()
    duration = state["duration_minutes"]
    ends_at = datetime.datetime.now(timezone.utc) + timedelta(minutes=duration)
    set_hunt_ends_at(ends_at.isoformat())
    return {"message": f"Hunt started — ends in {duration} minutes."}

@app.post("/admin/hunt/extend")
async def admin_hunt_extend(_: bool = Depends(require_admin)):
    """Add 5 minutes to the current hunt timer."""
    state = get_hunt_state()
    if not state["ends_at"]:
        return {"message": "Hunt is not running."}
    new_ends_at = state["ends_at"] + timedelta(minutes=5)
    set_hunt_ends_at(new_ends_at.isoformat())
    return {"message": "Hunt extended by 5 minutes."}

@app.post("/admin/hunt/stop")
async def admin_hunt_stop(_: bool = Depends(require_admin)):
    """Stop the hunt timer immediately."""
    set_hunt_ends_at("")
    return {"message": "Hunt stopped."}

@app.get("/admin/all-gallery")
async def admin_all_gallery(request: Request, _: bool = Depends(require_admin)):
    """Admin view: every submission from every team, with AI review details."""
    is_admin = request.session.get("is_admin") == True
    is_team = request.session.get("team_id") is not None
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
        base = os.path.splitext(os.path.basename(submission.thumbnail_path or ""))[0]
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
        request=request, name="admin_all_gallery.html",
        context={
            "entries": entries,
            "is_admin": is_admin,
            "is_team_member": is_team,
            "theme": await get_active_theme(),
            "hunt_state": get_hunt_state(),
        }
    )

@app.get("/admin/hints")
async def admin_hints(request: Request, _: bool = Depends(require_admin)):
    """Admin view: all hunt items with their bonus hints and AI criteria (private)."""
    is_admin = request.session.get("is_admin") == True
    is_team = request.session.get("team_id") is not None
    async with async_session() as session:
        result = await session.execute(
            select(HuntItem, Hunt.name)
            .join(Hunt, HuntItem.hunt_id == Hunt.id)
            .order_by(Hunt.name.asc(), HuntItem.id.asc())
        )
        rows = result.all()
    # Parse JSON property lists into Python lists for the template
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
        request=request, name="admin_hints.html",
        context={"items_with_hunts": items_with_hunts, "is_admin": is_admin, "is_team_member": is_team, "theme": await get_active_theme()}
    )

@app.get("/result")
async def result_page(request: Request, status: str, message: str):
    is_admin = request.session.get("is_admin") == True
    is_team = request.session.get("team_id") is not None
    return templates.TemplateResponse(
        request=request, name="result.html",
        context={"status": status, "message": message, "is_admin": is_admin, "is_team_member": is_team, "theme": await get_active_theme()}
    )

# --- Submission Logic ---

def parse_exif_datetime(value) -> datetime.datetime | None:
    """Parse EXIF datetime string 'YYYY:MM:DD HH:MM:SS' into a naive datetime.
    EXIF datetimes don't carry timezone info; we treat them as UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError):
        return None

def get_photo_taken_at(img: Image.Image) -> datetime.datetime | None:
    """Extract DateTimeOriginal (36867) -> DateTimeDigitized (36868) -> DateTime (306)
    If EXIF 2.31 OffsetTimeOriginal (0x9010) is present, convert to UTC and return
    a timezone-aware datetime. Otherwise return a naive datetime (treated as
    server local time by the caller). Returns None if no usable timestamp exists."""
    exif = img.getexif()
    if not exif:
        return None

    taken_at = None
    for tag_id in (36867, 36868, 306):
        val = exif.get(tag_id)
        if val:
            dt = parse_exif_datetime(val)
            if dt:
                taken_at = dt
                break
    if taken_at is None:
        return None

    # Try to read EXIF 2.31 timezone offset (OffsetTimeOriginal = 0x9010)
    # The EXIF sub-IFD is at IFD tag 0x8769 (ExifTags.IFD.Exif)
    exif_sub_ifd = exif.get_ifd(ExifTags.IFD.Exif) or {}
    offset_str = exif_sub_ifd.get(0x9010)
    if isinstance(offset_str, bytes):
        offset_str = offset_str.decode('utf-8', errors='ignore')

    if offset_str and isinstance(offset_str, str):
        try:
            sign = 1 if offset_str[0] == '+' else -1
            # Accept ±HH:MM or ±HHMM
            rest = offset_str[1:]
            if ':' in rest:
                h_str, m_str = rest.split(':', 1)
                h, m = int(h_str), int(m_str)
            else:
                h, m = int(rest[:2]), int(rest[2:]) if len(rest) >= 2 else 0
            offset = timedelta(hours=h, minutes=m) * sign
            # taken_at is local; convert to UTC
            return (taken_at - offset).replace(tzinfo=timezone.utc)
        except (ValueError, IndexError, AttributeError):
            pass

    return taken_at  # naive; caller treats as server local time

def make_thumbnails(original_path: str, base_name: str) -> tuple[str, str]:
    thumb_out  = os.path.join(THUMB_DIR,  f"{base_name}.jpg")
    display_out = os.path.join(DISPLAY_DIR, f"{base_name}.jpg")

    with Image.open(original_path) as img:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        t = img.copy()
        t.thumbnail((200, 200), Image.LANCZOS)
        t.save(thumb_out, "JPEG", quality=85, optimize=True)

        d = img.copy()
        d.thumbnail((1200, 1200), Image.LANCZOS)
        d.save(display_out, "JPEG", quality=90, optimize=True)

    return thumb_out, display_out

@app.post("/submit")
async def submit_photo(item_id: int = Form(...), team_id: int = Form(...), file: UploadFile = File(...)):
    # Gate: hunt must be open to accept submissions
    if not get_hunt_state()["is_open"]:
        logger.info(f"Submission REJECTED: Hunt is not currently open (team_id={team_id}, item_id={item_id})")
        return {
            "message": "Hunt closed",
            "redirect": "/result?status=error&message=The hunt is not currently open. Please wait for the admin to start it."
        }

    original_path = thumb_path = display_path = None

    try:
        async with async_session() as session:
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar()
            team_name = team.name if team else f"Unknown Team (ID: {team_id})"
            item = (await session.execute(select(HuntItem).where(HuntItem.id == item_id))).scalar()

        if not item:
            logger.error(f"Submission REJECTED: Team '{team_name}' submitted for non-existent Item ID {item_id}")
            return {"message": "Item not found", "redirect": "/result?status=error&message=Invalid item."}
        item_name = item.name

        contents = await file.read()
        if not contents:
            logger.warning(f"Submission REJECTED: Empty file from Team '{team_name}'")
            return {"message": "Empty file", "redirect": "/result?status=error&message=The uploaded file is empty."}

        filename = f"{uuid.uuid4().hex}_{file.filename}"
        original_path = os.path.join(ORIGINALS_DIR, filename)
        base_name = os.path.splitext(filename)[0]

        with open(original_path, "wb") as buffer:
            buffer.write(contents)

        thumb_path, display_path = make_thumbnails(original_path, base_name)

        # Pre-check: photo freshness (EXIF timestamp vs now)
        max_age_hours = get_max_photo_age_hours()
        try:
            with Image.open(original_path) as _img:
                taken_at = get_photo_taken_at(_img)
        except Exception:
            taken_at = None

        if taken_at is None:
            _cleanup_files(original_path, thumb_path, display_path)
            logger.info(f"Submission REJECTED (No EXIF timestamp): Team '{team_name}' — '{item_name}'")
            return {"message": "No timestamp", "redirect": "/result?status=error&message=Photo has no EXIF timestamp. Ensure your camera's date/time is enabled."}

        # Compare freshness:
        #   - If EXIF had OffsetTimeOriginal (0x9010), taken_at is timezone-aware UTC.
        #     Compare against current UTC for an exact, correct age.
        #   - If no offset tag, taken_at is naive (treated as server local time).
        #     Compare against current local time so the wall-clock feels right.
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
            return {"message": "Photo too old", "redirect": f"/result?status=error&message=Photo is {hours}h {minutes}m old. Photos must be less than {max_age_hours:g} hours old."}

        # Future timestamps (camera clock set wrong): warn but accept
        if age < timedelta(hours=-1):
            logger.warning(f"Photo timestamp is in the future: Team '{team_name}' — '{item_name}' "
                           f"taken at {taken_at} ({tz_label}), current {now_ref}, age {age}")

        # Pre-check: locked or out of attempts
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
                return {"message": "Item locked", "redirect": "/result?status=error&message=Your team already has an approved submission for this item."}

            attempt_count = (await session.execute(
                select(func.count(Submission.id)).where(
                    Submission.team_id == team_id,
                    Submission.item_id == item_id
                )
            )).scalar()
            if attempt_count >= MAX_ATTEMPTS_PER_ITEM:
                _cleanup_files(original_path, thumb_path, display_path)
                logger.info(f"Submission REJECTED (Max Attempts): Team '{team_name}' used all {MAX_ATTEMPTS_PER_ITEM} attempts on '{item_name}'")
                return {"message": "Max attempts", "redirect": f"/result?status=error&message=Maximum {MAX_ATTEMPTS_PER_ITEM} attempts reached for this item."}

        # AI Review (the SOLE gate now)
        required = json.loads(item.required_properties or "[]")
        bonus = json.loads(item.bonus_properties or "[]")
        review = review_image(original_path, item.name, item.description or "", required, bonus)

        all_required_met = (
            review["is_target"]
            and review["confidence"] >= CONFIDENCE_THRESHOLD
            and len(review["missed_required"]) == 0
        )
        all_bonus_met = all_required_met and len(review["missed_bonus"]) == 0 and len(bonus) > 0
        awarded_bonus = item.bonus_points if all_bonus_met else 0
        points_awarded = item.points + awarded_bonus if all_required_met else 0
        is_locked = all_required_met

        attempt_number = attempt_count + 1

        # Persist submission (even on fail — keep the photo for audit)
        async with async_session() as session:
            sub = Submission(
                item_id=item_id, team_id=team_id,
                photo_path=original_path,
                thumbnail_path=thumb_path,
                display_path=display_path,
                verified=is_locked,
                ai_reviewed=True,
                ai_review_result=json.dumps(review),
                ai_bonus_awarded=awarded_bonus,
                points_awarded=points_awarded,
                attempt_number=attempt_number,
            )
            session.add(sub)
            await session.commit()

        if is_locked:
            bonus_text = f" (+{awarded_bonus} bonus for matching all bonus properties!)" if awarded_bonus > 0 else ""
            logger.info(f"Submission APPROVED: Team '{team_name}' verified '{item_name}' on attempt {attempt_number}/{MAX_ATTEMPTS_PER_ITEM}. "
                        f"Points: {points_awarded}. Reason: {review['reason']}")
            return {
                "message": f"Verified! You earned {points_awarded} points{bonus_text}",
                "redirect": f"/result?status=success&message=Verified! You earned {points_awarded} points{bonus_text}"
            }

        logger.info(f"Submission REJECTED: Team '{team_name}' — '{item_name}' attempt {attempt_number}/{MAX_ATTEMPTS_PER_ITEM}. "
                    f"Reason: {review['reason']} | Confidence: {review['confidence']}")
        reason = review.get("reason", "The photo doesn't appear to match the target item.")
        return {
            "message": f"Not approved: {reason}",
            "redirect": f"/result?status=error&message=Not approved: {reason} (Attempt {attempt_number}/{MAX_ATTEMPTS_PER_ITEM})"
        }

    except Image.UnidentifiedImageError:
        logger.warning(f"Submission REJECTED (Unsupported Format): {file.filename if file else '?'}")
        _cleanup_files(original_path, thumb_path, display_path)
        return {"message": "Unsupported image format.", "redirect": "/result?status=error&message=The file format is not supported."}

    except Exception:
        logger.exception("Submission REJECTED (Server Error)")
        _cleanup_files(original_path, thumb_path, display_path)
        return {"message": "Server error", "redirect": "/result?status=error&message=An unexpected error occurred."}