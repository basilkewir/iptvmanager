from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from contextlib import asynccontextmanager
import logging

from app.config import settings
from app.database import init_db
from app.engine import engine
from app.routes.auth import router as auth_router
from app.routes.streams import router as streams_router, ws_router

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL), format="%(asctime)s %(name)-12s %(levelname)-8s %(message)s")

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

# Serve static frontend
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse("static/index.html")
