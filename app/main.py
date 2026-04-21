from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from contextlib import asynccontextmanager
import logging
import time

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

# Serve HLS segments
import os as _os
_os.makedirs(settings.HLS_OUTPUT_DIR, exist_ok=True)
app.mount("/hls", StaticFiles(directory=settings.HLS_OUTPUT_DIR), name="hls")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse("static/index.html")
