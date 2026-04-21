from pydantic import BaseModel
from typing import Optional

class UserCreate(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class StreamCreate(BaseModel):
    name: str
    source_url: str
    rtmp_key: str
    enabled: bool = True
    dvr_enabled: bool = True
    dvr_hours: int = 2
    logo_x: int = 10
    logo_y: int = 10

class StreamUpdate(BaseModel):
    name: Optional[str] = None
    source_url: Optional[str] = None
    rtmp_key: Optional[str] = None
    enabled: Optional[bool] = None
    dvr_enabled: Optional[bool] = None
    dvr_hours: Optional[int] = None
    logo_x: Optional[int] = None
    logo_y: Optional[int] = None

class StreamOut(BaseModel):
    id: int
    name: str
    source_url: str
    rtmp_key: str
    enabled: bool
    status: str
    dvr_enabled: bool
    dvr_hours: int
    udp_target: Optional[str] = None
    hls_url: Optional[str] = None
    rtmp_url: Optional[str] = None
    last_online: Optional[str] = None
    consecutive_failures: int = 0
    logo_path: Optional[str] = None
    logo_x: int = 10
    logo_y: int = 10
    dvr_segments: int = 0
    dvr_size_mb: float = 0.0
    recorder_running: bool = False

    class Config:
        from_attributes = True
