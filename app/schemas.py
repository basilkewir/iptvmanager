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
    dvr_hours: int = 24

class StreamUpdate(BaseModel):
    name: Optional[str] = None
    source_url: Optional[str] = None
    rtmp_key: Optional[str] = None
    enabled: Optional[bool] = None
    dvr_enabled: Optional[bool] = None
    dvr_hours: Optional[int] = None

class StreamOut(BaseModel):
    id: int
    name: str
    source_url: str
    rtmp_key: str
    enabled: bool
    status: str
    dvr_enabled: bool
    dvr_hours: int
    last_online: Optional[str] = None
    consecutive_failures: int = 0

    class Config:
        from_attributes = True
