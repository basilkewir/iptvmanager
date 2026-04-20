import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    APP_NAME: str = "IPTV Manager"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    SECRET_KEY: str = "change-me-in-production-use-openssl-rand-hex-32"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24h
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/iptvmanager.db"
    DVR_STORAGE_PATH: str = "./data/dvr"
    HEALTH_CHECK_INTERVAL: int = 5  # seconds
    HEALTH_CHECK_TIMEOUT: int = 15  # seconds (HLS needs more time)
    HEALTH_CHECK_FAILURES_BEFORE_DOWN: int = 3
    DVR_SEGMENT_DURATION: int = 10  # seconds per .ts segment
    DVR_RETENTION_HOURS: int = 24
    FLUSSONIC_RTMP_BASE: str = "rtmp://localhost/live"
    FFMPEG_PATH: str = "ffmpeg"
    FFPROBE_PATH: str = "ffprobe"
    LOG_LEVEL: str = "DEBUG"

    class Config:
        env_file = ".env"

settings = Settings()
os.makedirs(settings.DVR_STORAGE_PATH, exist_ok=True)
os.makedirs("data", exist_ok=True)
