from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import datetime

Base = declarative_base()

class Hunt(Base):
    __tablename__ = "hunts"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(String)
    theme_primary = Column(String, default="#FF6B9D")
    theme_secondary = Column(String, default="#FEC868")
    theme_accent = Column(String, default="#7AC74F")
    theme_background = Column(String, default="#FFF8F0")
    theme_surface = Column(String, default="#FFFFFF")
    theme_text = Column(String, default="#2D3142")
    theme_text_muted = Column(String, default="#6C757D")
    theme_success = Column(String, default="#06A77D")
    theme_warning = Column(String, default="#E63946")
    theme_info = Column(String, default="#4A90E2")
    items = relationship("HuntItem", back_populates="hunt", cascade="all, delete-orphan")

class HuntItem(Base):
    __tablename__ = "hunt_items"
    id = Column(Integer, primary_key=True)
    hunt_id = Column(Integer, ForeignKey("hunts.id"))
    name = Column(String, nullable=False)
    description = Column(String)
    points = Column(Integer, default=0)
    bonus_points = Column(Integer, default=0)
    required_properties = Column(String)   # JSON list[str]
    bonus_properties = Column(String)      # JSON list[str]
    bonus_hint = Column(String)            # optional hint for hunters
    hunt = relationship("Hunt", back_populates="items")

class Team(Base):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(String)
    passphrase = Column(String, unique=True, nullable=False)

class Submission(Base):
    __tablename__ = "submissions"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("hunt_items.id"))
    team_id = Column(Integer, ForeignKey("teams.id"))
    photo_path = Column(String)
    thumbnail_path = Column(String)
    display_path = Column(String)
    submitted_at = Column(DateTime, default=datetime.datetime.utcnow)
    verified = Column(Boolean, default=False)   # True when required_properties all matched
    ai_reviewed = Column(Boolean, default=False)
    ai_review_result = Column(String)            # JSON: matched/missed required+bonus
    ai_bonus_awarded = Column(Integer, default=0)
    points_awarded = Column(Integer, default=0)
    attempt_number = Column(Integer, default=1)