from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.database import get_db
from app.models import Stream, StreamLog, User
from app.schemas import StreamCreate, StreamUpdate, StreamOut
from app.auth import get_current_user
from app.engine import engine

router = APIRouter(prefix="/api/streams", tags=["streams"])

def _udp_for(s: Stream) -> str:
    from app.config import settings
    port = settings.UDP_MULTICAST_PORT_START + s.id
    return f"{settings.UDP_MULTICAST_BASE}:{port}"

def _stream_out(s: Stream) -> StreamOut:
    return StreamOut(
        id=s.id, name=s.name, source_url=s.source_url, rtmp_key=s.rtmp_key,
        enabled=s.enabled, status=s.status.value if s.status else "stopped",
        dvr_enabled=s.dvr_enabled, dvr_hours=s.dvr_hours,
        udp_target=_udp_for(s),
        last_online=s.last_online.isoformat() if s.last_online else None,
        consecutive_failures=s.consecutive_failures or 0,
    )

@router.get("/", response_model=List[StreamOut])
async def list_streams(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Stream))
    return [_stream_out(s) for s in result.scalars().all()]

@router.post("/", response_model=StreamOut)
async def create_stream(body: StreamCreate, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    s = Stream(**body.model_dump())
    db.add(s)
    await db.commit()
    await db.refresh(s)
    if s.enabled:
        await engine.add_stream(s)
    return _stream_out(s)

@router.put("/{stream_id}", response_model=StreamOut)
async def update_stream(stream_id: int, body: StreamUpdate, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Stream).where(Stream.id == stream_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Stream not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    await db.commit()
    await db.refresh(s)
    await engine.update_stream(s)
    return _stream_out(s)

@router.delete("/{stream_id}")
async def delete_stream(stream_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Stream).where(Stream.id == stream_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Stream not found")
    await engine.remove_stream(stream_id)
    await db.execute(delete(Stream).where(Stream.id == stream_id))
    await db.commit()
    return {"ok": True}

@router.get("/status")
async def all_status(_: User = Depends(get_current_user)):
    return engine.get_all_status()

@router.get("/{stream_id}/logs")
async def stream_logs(stream_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(
        select(StreamLog).where(StreamLog.stream_id == stream_id).order_by(StreamLog.created_at.desc()).limit(200)
    )
    logs = result.scalars().all()
    return [{"id": l.id, "event": l.event, "message": l.message, "created_at": l.created_at.isoformat()} for l in logs]


# ── WebSocket for real-time status ───────────────────────────────────────
ws_router = APIRouter()

@ws_router.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    await ws.accept()
    engine.register_ws(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        engine.unregister_ws(ws)
