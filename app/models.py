from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
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
    hunt = relationship("Hunt", back_populates="items")


class Team(Base):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
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
    verified = Column(Boolean, default=False)
    ai_review_result = Column(String)
    ai_bonus_awarded = Column(Integer, default=0)
    points_awarded = Column(Integer, default=0)
    attempt_number = Column(Integer, default=1)