from sqlalchemy import Column, Integer, String, DateTime, Boolean, Float, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
import datetime

Base = declarative_base()


class Hunt(Base):
    __tablename__ = "hunts"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    theme_json = Column(String, default="{}")
    items = relationship("HuntItem", back_populates="hunt", cascade="all, delete-orphan")


class HuntItem(Base):
    __tablename__ = "hunt_items"
    id = Column(Integer, primary_key=True)
    hunt_id = Column(Integer, ForeignKey("hunts.id"))
    name = Column(String, nullable=False)
    description = Column(String)
    points = Column(Integer, default=0)
    bonus_points = Column(Integer, default=0)
    required_properties = Column(String)
    bonus_properties = Column(String)
    bonus_hint = Column(String)
    # --- Coordinate judging (§2.1) ---
    target_lat = Column(Float, nullable=True)          # true target, used for judging
    target_lon = Column(Float, nullable=True)
    radius_meters = Column(Float, default=100.0)       # "hit" ring
    near_multiplier = Column(Float, default=2.0)       # outer "near" ring = radius × this
    proximity_points = Column(Integer, default=0)      # points for a full hit
    proximity_mode = Column(String, default="bonus")   # off | bonus | gate
    # Centre/radius of the geofence area drawn on the map (phase 2). May be
    # offset from the true target so the exact spot is not given away.
    zone_lat = Column(Float, nullable=True)
    zone_lon = Column(Float, nullable=True)
    zone_radius_meters = Column(Float, nullable=True)
    hunt = relationship("Hunt", back_populates="items")


class Team(Base):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    # PBKDF2 hash (see app/security.py) — never the plaintext passphrase (§3.2).
    passphrase_hash = Column(String, unique=True, nullable=False)


class TeamPosition(Base):
    """Live team position for the map — display only, never scored.
    
    Untrusted display data: it is never scored from and never used in judging
    (§2.5). Only the latest position per team is kept — the POST handler prunes
    prior rows for the team inside the same transaction, so the table holds at
    most one row per team for the whole event (see ``POST /api/position``).
    """

    __tablename__ = "team_positions"
    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    accuracy_m = Column(Float, nullable=True)
    recorded_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))
    source = Column(String, default="app")


class Submission(Base):
    __tablename__ = "submissions"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("hunt_items.id"))
    team_id = Column(Integer, ForeignKey("teams.id"))
    photo_path = Column(String)
    thumbnail_path = Column(String)
    display_path = Column(String)
    submitted_at = Column(DateTime, default=datetime.datetime.utcnow)
    verified = Column(Boolean, default=False)
    ai_review_result = Column(String)
    ai_bonus_awarded = Column(Integer, default=0)
    points_awarded = Column(Integer, default=0)
    attempt_number = Column(Integer, default=1)
    # --- §1.1: the EXIF capture instant (previously computed then discarded) ---
    captured_at = Column(DateTime, nullable=True)
    # --- §2.1 / §2.2: coordinates read server-side from the stored original ---
    gps_lat = Column(Float, nullable=True)
    gps_lon = Column(Float, nullable=True)
    gps_accuracy_m = Column(Float, nullable=True)
    distance_m = Column(Float, nullable=True)
    location_status = Column(String, default="no_target")
    proximity_points_awarded = Column(Integer, default=0)
    # --- §2.5: untrusted client-supplied live fix (display only, never scored) ---
    live_lat = Column(Float, nullable=True)
    live_lon = Column(Float, nullable=True)
