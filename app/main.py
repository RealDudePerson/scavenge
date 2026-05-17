from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request, status, responses
from fastapi.templating import Jinja2Templates
from fastapi.middleware.sessions import SessionMiddleware
from sqlalchemy.future import select
from sqlalchemy import delete, func
from .database import async_session, init_db
from .models import Hunt, HuntItem, Team, Submission, Base
from .config import HUNTS_DIR, TEAMS_DIR, UPLOADS_DIR
import yaml
import os
import datetime
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="super_secret_key_change_me")
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
async def startup():
    await init_db()

def get_admin_password():
    with open("admin_config.yaml", "r") as f:
        return yaml.safe_load(f)["admin_password"]

def is_admin(request: Request):
    if request.session.get("is_admin") != True:
        raise HTTPException(status_code=403, detail="Not authorized")

# --- Admin Endpoints ---

@app.get("/admin/login")
async def admin_login_page(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if password == get_admin_password():
        request.session["is_admin"] = True
        return responses.RedirectResponse(url="/admin/dashboard", status_code=303)
    raise HTTPException(status_code=401, detail="Invalid password")

@app.get("/admin/dashboard", dependencies=[Depends(is_admin)])
async def admin_dashboard(request: Request):
    return templates.TemplateResponse("admin_dashboard.html", {"request": request})

@app.post("/admin/reset", dependencies=[Depends(is_admin)])
async def reset_db():
    from .database import engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    # Clear uploaded files
    for filename in os.listdir(UPLOADS_DIR):
        file_path = os.path.join(UPLOADS_DIR, filename)
        os.remove(file_path)
    return {"message": "All progress and teams reset."}

@app.post("/admin/load-hunts", dependencies=[Depends(is_admin)])
async def load_hunts():
    # Load YAML files from hunts/
    for filename in os.listdir(HUNTS_DIR):
        if filename.endswith(".yaml"):
            with open(os.path.join(HUNTS_DIR, filename), "r") as f:
                data = yaml.safe_load(f)
                async with async_session() as session:
                    hunt = Hunt(name=data['name'], description=data['description'])
                    session.add(hunt)
                    await session.commit()
                    for item_data in data['items']:
                        item = HuntItem(
                            hunt_id=hunt.id,
                            name=item_data['name'],
                            description=item_data['description'],
                            points=item_data['points'],
                            lat=item_data['location']['lat'],
                            lon=item_data['location']['lon'],
                            radius=item_data['radius_meters']
                        )
                        session.add(item)
                    await session.commit()
    return {"message": "Hunts loaded."}

@app.post("/admin/load-teams", dependencies=[Depends(is_admin)])
async def load_teams():
    with open(os.path.join(TEAMS_DIR, "teams.yaml"), "r") as f:
        data = yaml.safe_load(f)
        async with async_session() as session:
            for team_data in data['teams']:
                team = Team(name=team_data['name'], description=team_data['description'], passphrase=team_data['passphrase'])
                session.add(team)
            await session.commit()
    return {"message": "Teams loaded."}

# --- Team Endpoints ---

@app.post("/team/login")
async def login(passphrase: str = Form(...)):
    async with async_session() as session:
        result = await session.execute(select(Team).where(Team.passphrase == passphrase))
        team = result.scalar()
        if not team:
            raise HTTPException(status_code=401, detail="Invalid passphrase")
        return {"team_id": team.id, "name": team.name}

# --- Submission Logic ---

def get_geotagging(exif_data):
    if not exif_data:
        return None
    geotagging = {}
    for (idx, tag) in TAGS.items():
        if tag == 'GPSInfo':
            if idx not in exif_data:
                return None
            for key in exif_data[idx].keys():
                decode = GPSTAGS.get(key, key)
                geotagging[decode] = exif_data[idx][key]
    return geotagging

def get_decimal_from_dms(dms, ref):
    degrees = dms[0]
    minutes = dms[1]
    seconds = dms[2]
    val = float(degrees) + (float(minutes) / 60.0) + (float(seconds) / 3600.0)
    if ref in ['S', 'W']:
        val = -val
    return val

@app.post("/submit")
async def submit_photo(item_id: int = Form(...), team_id: int = Form(...), file: UploadFile = File(...)):
    # 1. Save photo locally
    file_path = os.path.join(UPLOADS_DIR, f"{datetime.datetime.utcnow().timestamp()}_{file.filename}")
    with open(file_path, "wb") as buffer:
        buffer.write(await file.read())
    
    # 2. Extract EXIF
    img = Image.open(file_path)
    exif = img._getexif()
    geotagging = get_geotagging(exif)
    
    if not geotagging:
        raise HTTPException(status_code=400, detail="No GPS data found")

    lat = get_decimal_from_dms(geotagging['GPSLatitude'], geotagging['GPSLatitudeRef'])
    lon = get_decimal_from_dms(geotagging['GPSLongitude'], geotagging['GPSLongitudeRef'])
    
    # 3. Verify (Simplified distance check)
    async with async_session() as session:
        item = (await session.execute(select(HuntItem).where(HuntItem.id == item_id))).scalar()
        
        # Distance calculation (simple Euclidean for now)
        dist = ((lat - item.lat)**2 + (lon - item.lon)**2)**0.5 * 111000 # Rough conversion to meters
        
        if dist <= item.radius:
            sub = Submission(item_id=item_id, team_id=team_id, photo_path=file_path, verified=True)
            session.add(sub)
            await session.commit()
            return {"message": "Success!"}
        
    return {"message": "Outside of radius"}

# --- Leaderboard ---

@app.get("/leaderboard")
async def get_leaderboard():
    async with async_session() as session:
        # Get team scores
        result = await session.execute(
            select(Team.name, func.sum(HuntItem.points))
            .join(Submission, Team.id == Submission.team_id)
            .join(HuntItem, Submission.item_id == HuntItem.id)
            .where(Submission.verified == True)
            .group_by(Team.name)
        )
        return {"leaderboard": result.all()}

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
