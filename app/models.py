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
    items = relationship("HuntItem", back_populates="hunt", cascade="all, delete-orphan")

class HuntItem(Base):
    __tablename__ = "hunt_items"
    id = Column(Integer, primary_key=True)
    hunt_id = Column(Integer, ForeignKey("hunts.id"))
    name = Column(String, nullable=False)
    description = Column(String)
    points = Column(Integer, default=0)
    lat = Column(Float)
    lon = Column(Float)
    radius = Column(Float)
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
    submitted_at = Column(DateTime, default=datetime.datetime.utcnow)
    verified = Column(Boolean, default=False)
