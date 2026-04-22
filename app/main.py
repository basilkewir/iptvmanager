from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response, StreamingResponse
from contextlib import asynccontextmanager
import asyncio
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

@app.get("/api/test-outputs/{stream_id}")
async def test_outputs(stream_id: int):
    """Test all output URLs for a stream to verify they're accessible."""
    sp = engine.streams.get(stream_id)
    if not sp:
        return JSONResponse({"error": f"Stream {stream_id} not found"}, status_code=404)

    results = {
        "stream_id": stream_id,
        "stream_name": sp.name,
        "udp_target": sp.udp_target,
        "udp_receive": f"udp://@{sp.udp_target.replace('udp://', '').split('?')[0]}",
        "rtmp_target": sp.rtmp_target,
        "hls_url": f"http://localhost:{settings.APP_PORT}/hls/{stream_id}/index.m3u8",
        "flussonic_ts_url": f"ts+http://localhost:{settings.APP_PORT}/ts/{stream_id}",
        "tests": {}
    }

    # Test HLS playlist
    hls_path = os.path.join(settings.HLS_OUTPUT_DIR, str(stream_id), "index.m3u8")
    hls_exists = os.path.exists(hls_path)
    hls_size = os.path.getsize(hls_path) if hls_exists else 0
    results["tests"]["hls_playlist"] = {
        "exists": hls_exists,
        "size": hls_size,
        "status": "ok" if hls_size > 0 else ("empty" if hls_exists else "missing")
    }

    # Test UDP process
    results["tests"]["udp_process"] = {
        "running": sp.output_process is not None and sp.output_process.returncode is None
    }

    # Test RTMP process
    results["tests"]["rtmp_process"] = {
        "running": sp.rtmp_process is not None and sp.rtmp_process.returncode is None
    }

    # Test DVR segments
    dvr_segments = len(sp._get_recent_segments()) if hasattr(sp, '_get_recent_segments') else 0
    results["tests"]["dvr_segments"] = {"count": dvr_segments}

    return JSONResponse(results)

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

# ── HTTP MPEG-TS streaming endpoint ──────────────────────────────────────────
# Flussonic uses "ts+http://server:port/ts/{stream_id}" as its input format.
# This endpoint spawns FFmpeg to read the UDP output and pipe raw MPEG-TS over HTTP.
# One FFmpeg relay process is created per connecting client (Flussonic = 1 connection).


@app.get("/ts/{stream_id}")
async def ts_http_stream(stream_id: int):
    """
    HTTP MPEG-TS output — use as: ts+http://YOUR_SERVER_IP:8000/ts/{stream_id}
    Compatible with Flussonic (ts+http://), tvheadend, VLC, ffplay, and all IPTV middleware.

    Reads from the HLS output (which is always available when the stream is live),
    and re-streams as raw MPEG-TS over HTTP for maximum compatibility.
    """
    sp = engine.streams.get(stream_id)
    if not sp:
        return Response(status_code=404, content=f"Stream {stream_id} not found")

    # Check HLS playlist exists (means stream is actually outputting)
    hls_playlist = os.path.join(settings.HLS_OUTPUT_DIR, str(stream_id), "index.m3u8")
    if not os.path.exists(hls_playlist) or os.path.getsize(hls_playlist) == 0:
        return Response(
            status_code=503,
            content="Stream not ready — HLS output not yet available. Wait a few seconds and retry."
        )

    # Read from HLS (working) rather than UDP (sending only, can't easily receive back)
    hls_url = f"http://127.0.0.1:{settings.APP_PORT}/hls/{stream_id}/index.m3u8"

    async def generate():
        proc = await asyncio.create_subprocess_exec(
            settings.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel", "error",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "3",
            "-i", hls_url,
            "-c", "copy",
            "-map", "0",
            "-f", "mpegts",
            "-mpegts_flags", "+resend_headers",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                chunk = await proc.stdout.read(65536)  # 64 KB chunks
                if not chunk:
                    break
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

    return StreamingResponse(
        generate(),
        media_type="video/MP2T",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

