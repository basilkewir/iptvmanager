from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from contextlib import asynccontextmanager
import logging
import time
import os

from app.config import settings
from app.database import init_db
from app.engine import engine
from app.routes.auth import router as auth_router
from app.routes.streams import router as streams_router, ws_router

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL), format="%(asctime)s %(name)-12s %(levelname)-8s %(message)s")

_start_time = time.time()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await engine.start()
    yield
    await engine.shutdown()

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.include_router(auth_router)
app.include_router(streams_router)
app.include_router(ws_router)

@app.get("/api/health")
async def health_check():
    """External health / liveness endpoint — no auth required."""
    uptime = int(time.time() - _start_time)
    statuses = engine.get_all_status()
    total = len(statuses)
    live = sum(1 for s in statuses if s["status"] == "live")
    dvr = sum(1 for s in statuses if s["status"] == "dvr")
    down = sum(1 for s in statuses if s["status"] == "down")
    return JSONResponse({
        "status": "ok",
        "uptime_seconds": uptime,
        "streams": {"total": total, "live": live, "dvr": dvr, "down": down},
    })

# Serve static frontend
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── HLS Streaming endpoints ───────────────────────────────────────────────────
# These serve HLS playlists and segments with correct MIME types and CORS headers
# so they work natively with Flussonic, VLC, ffplay, and all IPTV clients.
# Flussonic requires:
#   - Content-Type: application/vnd.apple.mpegurl  (for .m3u8)
#   - Content-Type: video/MP2T                     (for .ts segments)
#   - Access-Control-Allow-Origin: *               (CORS for cross-origin clients)
#   - Cache-Control: no-cache, no-store            (live stream must not be cached)

os.makedirs(settings.HLS_OUTPUT_DIR, exist_ok=True)

_HLS_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
}

@app.options("/hls/{stream_id}/{filename}")
async def hls_options(stream_id: int, filename: str):
    """CORS preflight handler for HLS requests from Flussonic / browser players."""
    return Response(status_code=204, headers=_HLS_CORS_HEADERS)

@app.get("/hls/{stream_id}/index.m3u8")
async def hls_playlist(stream_id: int):
    """Serve HLS master playlist with correct MIME type for Flussonic compatibility."""
    path = os.path.join(settings.HLS_OUTPUT_DIR, str(stream_id), "index.m3u8")
    if not os.path.exists(path):
        return Response(status_code=404, content="HLS playlist not found — stream may not be running")
    with open(path, "rb") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            **_HLS_CORS_HEADERS,
            "Content-Length": str(len(content)),
        }
    )

@app.get("/hls/{stream_id}/{segment}")
async def hls_segment(stream_id: int, segment: str):
    """Serve HLS .ts segments with correct MIME type."""
    if not segment.endswith(".ts"):
        return Response(status_code=400, content="Invalid segment")
    path = os.path.join(settings.HLS_OUTPUT_DIR, str(stream_id), segment)
    if not os.path.exists(path):
        return Response(status_code=404, content="Segment not found")
    with open(path, "rb") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="video/MP2T",
        headers={
            **_HLS_CORS_HEADERS,
            "Content-Length": str(len(content)),
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse("static/index.html")
