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
    HEALTH_CHECK_TIMEOUT: int = 30  # seconds
    HEALTH_CHECK_FAILURES_BEFORE_DOWN: int = 2  # 2×5s = 10s before DVR failover
    DVR_SEGMENT_DURATION: int = 6  # seconds per .ts segment
    DVR_RETENTION_HOURS: int = 2   # keep 2 hours of DVR
    UDP_MULTICAST_BASE: str = "udp://239.0.0.1"
    UDP_MULTICAST_PORT_START: int = 5000
    UDP_TTL: int = 16
    UDP_BUFFER_SIZE: int = 1316
    FFMPEG_PATH: str = "ffmpeg"
    FFPROBE_PATH: str = "ffprobe"
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"

settings = Settings()
os.makedirs(settings.DVR_STORAGE_PATH, exist_ok=True)
os.makedirs("data", exist_ok=True)
