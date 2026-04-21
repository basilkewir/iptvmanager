from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
import os
import shutil
import httpx

from app.database import get_db
from app.models import Stream, StreamLog, User
from app.schemas import StreamCreate, StreamUpdate, StreamOut
from app.auth import get_current_user
from app.engine import engine

LOGO_DIR = os.path.join("data", "logos")
os.makedirs(LOGO_DIR, exist_ok=True)

router = APIRouter(prefix="/api/streams", tags=["streams"])

def _udp_for(s: Stream) -> str:
    from app.config import settings
    port = settings.UDP_MULTICAST_PORT_START + s.id
    return f"{settings.UDP_MULTICAST_BASE}:{port}"

def _hls_for(s: Stream) -> str:
    return f"/hls/{s.id}/index.m3u8"

def _rtmp_for(s: Stream) -> str:
    from app.config import settings
    if s.rtmp_key:
        return f"{settings.RTMP_SERVER_URL}/{s.rtmp_key}"
    return None

def _stream_out(s: Stream, dvr_segs: int = 0, dvr_size_mb: float = 0.0,
                recorder_running: bool = False) -> StreamOut:
    return StreamOut(
        id=s.id, name=s.name, source_url=s.source_url, rtmp_key=s.rtmp_key,
        enabled=s.enabled, status=s.status.value if s.status else "stopped",
        dvr_enabled=s.dvr_enabled, dvr_hours=s.dvr_hours,
        udp_target=_udp_for(s),
        hls_url=_hls_for(s),
        rtmp_url=_rtmp_for(s),
        last_online=s.last_online.isoformat() if s.last_online else None,
        consecutive_failures=s.consecutive_failures or 0,
        logo_path=s.logo_path,
        logo_x=s.logo_x if s.logo_x is not None else 10,
        logo_y=s.logo_y if s.logo_y is not None else 10,
        dvr_segments=dvr_segs,
        dvr_size_mb=dvr_size_mb,
        recorder_running=recorder_running,
    )

@router.get("/", response_model=List[StreamOut])
async def list_streams(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Stream))
    # Compute DVR stats once from engine (one pass over in-memory state)
    engine_status = {e["stream_id"]: e for e in engine.get_all_status()}
    out = []
    for s in result.scalars().all():
        es = engine_status.get(s.id, {})
        out.append(_stream_out(
            s,
            dvr_segs=es.get("dvr_segments", 0),
            dvr_size_mb=es.get("dvr_size_mb", 0.0),
            recorder_running=es.get("recorder_running", False),
        ))
    return out

def _stream_out_single(s: Stream) -> StreamOut:
    """Build StreamOut for one stream, pulling live stats from engine."""
    sp = engine.streams.get(s.id)
    dvr_segs, dvr_size_mb, recorder_running = 0, 0.0, False
    if sp:
        segs = sp._get_recent_segments()
        dvr_segs = len(segs)
        try:
            dvr_size_mb = round(sum(os.path.getsize(f) for f in segs if os.path.exists(f)) / 1048576, 2)
        except Exception:
            pass
        recorder_running = sp.recorder_process is not None and sp.recorder_process.returncode is None
    return _stream_out(s, dvr_segs, dvr_size_mb, recorder_running)

@router.post("/", response_model=StreamOut)
async def create_stream(body: StreamCreate, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    s = Stream(**body.model_dump())
    db.add(s)
    await db.commit()
    await db.refresh(s)
    if s.enabled:
        await engine.add_stream(s)
    return _stream_out_single(s)

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
    return _stream_out_single(s)

@router.delete("/{stream_id}")
async def delete_stream(stream_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Stream).where(Stream.id == stream_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Stream not found")
    # Clean up logo file
    if s.logo_path and os.path.isfile(s.logo_path):
        try:
            os.remove(s.logo_path)
        except Exception:
            pass
    await engine.remove_stream(stream_id)
    await db.execute(delete(Stream).where(Stream.id == stream_id))
    await db.commit()
    return {"ok": True}

@router.post("/{stream_id}/logo", response_model=StreamOut)
async def upload_logo(stream_id: int,
                      file: Optional[UploadFile] = File(None),
                      logo_url: Optional[str] = Form(None),
                      db: AsyncSession = Depends(get_db),
                      _: User = Depends(get_current_user)):
    result = await db.execute(select(Stream).where(Stream.id == stream_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Stream not found")
    # Remove old logo
    if s.logo_path and os.path.isfile(s.logo_path):
        try:
            os.remove(s.logo_path)
        except Exception:
            pass
    if file and file.filename:
        # Upload from file browse
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            raise HTTPException(400, "Logo must be an image (png/jpg/bmp/webp)")
        logo_filename = f"{s.rtmp_key}{ext}"
        logo_full = os.path.join(LOGO_DIR, logo_filename)
        with open(logo_full, "wb") as f:
            shutil.copyfileobj(file.file, f)
    elif logo_url and logo_url.strip():
        # Download from URL
        logo_url = logo_url.strip()
        ext = os.path.splitext(logo_url.split("?")[0])[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            ext = ".png"
        logo_filename = f"{s.rtmp_key}{ext}"
        logo_full = os.path.join(LOGO_DIR, logo_filename)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(logo_url)
                r.raise_for_status()
                with open(logo_full, "wb") as f:
                    f.write(r.content)
        except Exception as e:
            raise HTTPException(400, f"Failed to download logo: {e}")
    else:
        raise HTTPException(400, "Provide a file or a logo_url")
    s.logo_path = os.path.abspath(logo_full)
    await db.commit()
    await db.refresh(s)
    await engine.update_stream(s)
    return _stream_out_single(s)

@router.delete("/{stream_id}/logo", response_model=StreamOut)
async def delete_logo(stream_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Stream).where(Stream.id == stream_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Stream not found")
    if s.logo_path and os.path.isfile(s.logo_path):
        try:
            os.remove(s.logo_path)
        except Exception:
            pass
    s.logo_path = None
    await db.commit()
    await db.refresh(s)
    await engine.update_stream(s)
    return _stream_out_single(s)

@router.get("/status")
async def all_status(_: User = Depends(get_current_user)):
    return engine.get_all_status()

@router.post("/{stream_id}/start")
async def start_stream(stream_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Stream).where(Stream.id == stream_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Stream not found")
    if not s.enabled:
        s.enabled = True
        await db.commit()
        await db.refresh(s)
        await engine.add_stream(s)
    else:
        await engine.start_stream(stream_id)
    return {"ok": True, "action": "started"}

@router.post("/{stream_id}/stop")
async def stop_stream(stream_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Stream).where(Stream.id == stream_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Stream not found")
    await engine.stop_stream(stream_id)
    return {"ok": True, "action": "stopped"}

@router.get("/dvr/summary")
async def dvr_summary(_: User = Depends(get_current_user)):
    return engine.get_dvr_summary()

@router.get("/{stream_id}/dvr")
async def stream_dvr(stream_id: int, _: User = Depends(get_current_user)):
    info = engine.get_stream_dvr_detail(stream_id)
    if info is None:
        raise HTTPException(404, "Stream not found in engine")
    return info

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
