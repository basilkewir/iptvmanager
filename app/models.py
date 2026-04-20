from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, Text, Enum as SAEnum
from sqlalchemy.sql import func
from app.database import Base
import enum

class StreamStatus(str, enum.Enum):
    LIVE = "live"
    DOWN = "down"
    DVR = "dvr"
    STOPPED = "stopped"

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Stream(Base):
    __tablename__ = "streams"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)
    source_url = Column(Text, nullable=False)
    rtmp_key = Column(String(100), nullable=False, unique=True)
    enabled = Column(Boolean, default=True)
    status = Column(SAEnum(StreamStatus), default=StreamStatus.STOPPED)
    dvr_enabled = Column(Boolean, default=True)
    dvr_hours = Column(Integer, default=24)
    last_online = Column(DateTime(timezone=True), nullable=True)
    last_checked = Column(DateTime(timezone=True), nullable=True)
    consecutive_failures = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class StreamLog(Base):
    __tablename__ = "stream_logs"
    id = Column(Integer, primary_key=True, index=True)
    stream_id = Column(Integer, nullable=False, index=True)
    event = Column(String(50), nullable=False)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
