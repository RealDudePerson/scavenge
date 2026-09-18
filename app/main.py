from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request, responses
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from sqlalchemy.future import select
from sqlalchemy import func
from .database import async_session, init_db
from .models import Hunt, HuntItem, Team, Submission, Base
from .config import HUNTS_DIR, TEAMS_DIR, ORIGINALS_DIR, THUMB_DIR, DISPLAY_DIR, get_admin_password, get_max_photo_age_hours, get_hunt_state, set_hunt_ends_at
from .ai_review import review_image
import logging
import json
import yaml
import os
import uuid
import datetime
from datetime import timezone, timedelta
from PIL import Image
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
app.mount("/uploads/originals", StaticFiles(directory="uploads/originals"), name="uploads-originals")
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
    global _ACTIVE_THEME
    try:
        async with async_session() as session:
            hunt = (await session.execute(select(Hunt).order_by(Hunt.id.asc()))).scalars().first()
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
    await _refresh_theme()
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
            team = Team(name=team_data['name'], passphrase=team_data['passphrase'])
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
async def hunt_dashboard(request: Request, _: None = Depends(require_team)):
    team_id = request.session.get("team_id")
    async with async_session() as session:
        items_result = await session.execute(select(HuntItem))
        items = items_result.scalars().all()

        subs_result = await session.execute(
            select(Submission).where(Submission.team_id == team_id)
        )
        subs = subs_result.scalars().all()

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
            request, "hunt_dashboard.html", _ctx(
                request,
                items=items,
                item_state=item_state,
                total_points=total_points,
                progress=progress,
                max_attempts=MAX_ATTEMPTS_PER_ITEM,
                team_id=team_id,
            )
        )


@app.get("/leaderboard")
async def get_leaderboard(request: Request):
    async with async_session() as session:
        rank_result = await session.execute(
            select(Team.name, func.sum(Submission.points_awarded))
            .join(Submission, Team.id == Submission.team_id)
            .where(Submission.verified == True)
            .group_by(Team.name)
            .order_by(func.sum(Submission.points_awarded).desc())
        )
        leaderboard = rank_result.all()

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


@app.get("/gallery")
async def gallery(request: Request, _: None = Depends(require_team)):
    team_id = request.session.get("team_id")
    async with async_session() as session:
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

def get_photo_taken_at(img: Image.Image) -> datetime.datetime | None:
    """Extract DateTimeOriginal (36867) -> DateTimeDigitized (36868) -> DateTime (306)
    If EXIF 2.31 OffsetTimeOriginal (0x9010) is present, convert to UTC and return
    a timezone-aware datetime. Otherwise return a naive datetime (treated as
    server local time by the caller). Returns None if no usable timestamp exists."""
    from PIL import ExifTags
    exif = img.getexif()
    if not exif:
        return None

    taken_at = None
    for tag_id in (36867, 36868, 306):
        val = exif.get(tag_id)
        if val:
            try:
                taken_at = datetime.datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
                break
            except (TypeError, ValueError):
                continue
    if taken_at is None:
        return None

    exif_sub_ifd = exif.get_ifd(ExifTags.IFD.Exif) or {}
    offset_str = exif_sub_ifd.get(0x9010)
    if isinstance(offset_str, bytes):
        offset_str = offset_str.decode('utf-8', errors='ignore')

    if offset_str and isinstance(offset_str, str):
        try:
            sign = 1 if offset_str[0] == '+' else -1
            rest = offset_str[1:]
            if ':' in rest:
                h_str, m_str = rest.split(':', 1)
                h, m = int(h_str), int(m_str)
            else:
                h, m = int(rest[:2]), int(rest[2:]) if len(rest) >= 2 else 0
            offset = timedelta(hours=h, minutes=m) * sign
            return (taken_at - offset).replace(tzinfo=timezone.utc)
        except (ValueError, IndexError, AttributeError):
            pass

    return taken_at


def make_thumbnails(original_path: str, base_name: str) -> None:
    """Generate thumb (200px) + display (1200px) JPEGs into the standard dirs."""
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


@app.post("/submit")
async def submit_photo(request: Request, item_id: int = Form(...), team_id: int = Form(...), file: UploadFile = File(...), _: None = Depends(require_team)):
    session_team_id = request.session.get("team_id")
    if session_team_id != team_id:
        logger.warning(f"Submission REJECTED: posted team_id {team_id} does not match logged-in team {session_team_id}")
        return {"message": "Team mismatch", "redirect": "/result?status=error&message=You are not logged in as that team."}

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
        thumb_path = os.path.join(THUMB_DIR, f"{base_name}.jpg")
        display_path = os.path.join(DISPLAY_DIR, f"{base_name}.jpg")

        with open(original_path, "wb") as buffer:
            buffer.write(contents)

        make_thumbnails(original_path, base_name)

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
        points_awarded = item.points + awarded_bonus if all_required_met else 0
        is_locked = all_required_met

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
                return {"message": "Item locked", "redirect": "/result?status=error&message=That item is no longer available for submission."}

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