"""
Stream Engine — the core controller that manages health checks, FFmpeg processes,
live→DVR failover, and RTMP push to Flussonic.
"""
import asyncio
import logging
import os
import signal
import time
import glob
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import Stream, StreamStatus, StreamLog

logger = logging.getLogger("engine")

# ── per-stream runtime state ────────────────────────────────────────────────
class StreamProcess:
    """Wraps the FFmpeg subprocess + metadata for one stream."""
    def __init__(self, stream_id: int, name: str, source_url: str, rtmp_key: str, dvr_hours: int):
        self.stream_id = stream_id
        self.name = name
        self.source_url = source_url
        self.rtmp_key = rtmp_key
        self.dvr_hours = dvr_hours
        self.process: Optional[asyncio.subprocess.Process] = None
        self.dvr_process: Optional[asyncio.subprocess.Process] = None
        self.mode: StreamStatus = StreamStatus.STOPPED
        self.consecutive_failures = 0
        self.last_online: Optional[datetime] = None
        self.lock = asyncio.Lock()

    @property
    def rtmp_target(self) -> str:
        return f"{settings.FLUSSONIC_RTMP_BASE}/{self.rtmp_key}"

    @property
    def dvr_dir(self) -> str:
        d = os.path.join(settings.DVR_STORAGE_PATH, self.rtmp_key)
        os.makedirs(d, exist_ok=True)
        return d

    # ── health check ─────────────────────────────────────────────────────
    async def check_health(self) -> bool:
        """Return True if source stream is alive."""
        try:
            # Quick ffprobe check
            proc = await asyncio.create_subprocess_exec(
                settings.FFPROBE_PATH,
                "-v", "error",
                "-rtsp_transport", "tcp",
                "-i", self.source_url,
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=settings.HEALTH_CHECK_TIMEOUT
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return False
            return proc.returncode == 0 and len(stdout.strip()) > 0
        except Exception as e:
            logger.warning(f"[{self.name}] health check error: {e}")
            return False

    # ── start live push ──────────────────────────────────────────────────
    async def start_live(self):
        async with self.lock:
            await self._kill_process()
            await self._kill_dvr_playback()
            logger.info(f"[{self.name}] Starting LIVE push → {self.rtmp_target}")
            cmd = [
                settings.FFMPEG_PATH,
                "-re", "-i", self.source_url,
                "-c", "copy",
                "-f", "flv",
                self.rtmp_target,
            ]
            self.process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            self.mode = StreamStatus.LIVE
            self.consecutive_failures = 0
            self.last_online = datetime.now(timezone.utc)

    # ── start DVR recording (only while live) ────────────────────────────
    async def start_dvr_recording(self):
        """Record live source to local .ts segments for DVR."""
        async with self.lock:
            await self._kill_dvr_recording()
            logger.info(f"[{self.name}] Starting DVR recording")
            seg_path = os.path.join(self.dvr_dir, "seg_%05d.ts")
            cmd = [
                settings.FFMPEG_PATH,
                "-i", self.source_url,
                "-c", "copy",
                "-f", "segment",
                "-segment_time", str(settings.DVR_SEGMENT_DURATION),
                "-reset_timestamps", "1",
                seg_path,
            ]
            self.dvr_process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )

    async def _kill_dvr_recording(self):
        if self.dvr_process and self.dvr_process.returncode is None:
            try:
                self.dvr_process.terminate()
                await asyncio.wait_for(self.dvr_process.wait(), timeout=5)
            except Exception:
                try:
                    self.dvr_process.kill()
                except Exception:
                    pass
            self.dvr_process = None

    # ── play DVR segments to RTMP (failover) ─────────────────────────────
    async def start_dvr_playback(self):
        """Loop recent DVR segments to RTMP so Flussonic keeps serving."""
        async with self.lock:
            await self._kill_process()
            await self._kill_dvr_recording()
            segments = self._get_recent_segments()
            if not segments:
                logger.warning(f"[{self.name}] No DVR segments available — nothing to play")
                self.mode = StreamStatus.DOWN
                return
            # Build a concat file
            concat_path = os.path.join(self.dvr_dir, "playlist.txt")
            with open(concat_path, "w") as f:
                for seg in segments:
                    f.write(f"file '{seg}'\n")
            logger.info(f"[{self.name}] Starting DVR playback ({len(segments)} segments) → {self.rtmp_target}")
            cmd = [
                settings.FFMPEG_PATH,
                "-re",
                "-stream_loop", "-1",       # loop forever until killed
                "-f", "concat", "-safe", "0",
                "-i", concat_path,
                "-c", "copy",
                "-f", "flv",
                self.rtmp_target,
            ]
            self.process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            self.mode = StreamStatus.DVR

    async def _kill_dvr_playback(self):
        """Kill the DVR playback (same process slot as live)."""
        await self._kill_process()

    # ── stop everything ──────────────────────────────────────────────────
    async def stop(self):
        async with self.lock:
            await self._kill_process()
            await self._kill_dvr_recording()
            self.mode = StreamStatus.STOPPED

    async def _kill_process(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

    # ── DVR segment helpers ──────────────────────────────────────────────
    def _get_recent_segments(self) -> list[str]:
        """Return .ts files within DVR retention window, sorted by mtime."""
        cutoff = time.time() - (self.dvr_hours * 3600)
        files = sorted(glob.glob(os.path.join(self.dvr_dir, "seg_*.ts")), key=os.path.getmtime)
        return [f for f in files if os.path.getmtime(f) >= cutoff]

    def cleanup_old_segments(self):
        cutoff = time.time() - (self.dvr_hours * 3600)
        for f in glob.glob(os.path.join(self.dvr_dir, "seg_*.ts")):
            if os.path.getmtime(f) < cutoff:
                try:
                    os.remove(f)
                except Exception:
                    pass


# ── Engine singleton ─────────────────────────────────────────────────────────
class Engine:
    def __init__(self):
        self.streams: Dict[int, StreamProcess] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ws_clients: list = []  # websocket broadcast list

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self):
        if self._running:
            return
        self._running = True
        # Load enabled streams from DB
        async with async_session() as db:
            result = await db.execute(select(Stream).where(Stream.enabled == True))
            for s in result.scalars().all():
                self._register(s)
        self._task = asyncio.create_task(self._loop())
        logger.info("Engine started")

    async def shutdown(self):
        self._running = False
        if self._task:
            self._task.cancel()
        for sp in self.streams.values():
            await sp.stop()
        logger.info("Engine stopped")

    # ── stream registration ───────────────────────────────────────────────
    def _register(self, s: Stream) -> StreamProcess:
        sp = StreamProcess(s.id, s.name, s.source_url, s.rtmp_key, s.dvr_hours)
        self.streams[s.id] = sp
        return sp

    async def add_stream(self, s: Stream):
        sp = self._register(s)
        # immediately start monitoring
        await self._check_and_act(sp)

    async def remove_stream(self, stream_id: int):
        sp = self.streams.pop(stream_id, None)
        if sp:
            await sp.stop()

    async def update_stream(self, s: Stream):
        await self.remove_stream(s.id)
        if s.enabled:
            await self.add_stream(s)

    # ── main loop ─────────────────────────────────────────────────────────
    async def _loop(self):
        while self._running:
            tasks = [self._check_and_act(sp) for sp in list(self.streams.values())]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # cleanup old DVR segments
            for sp in self.streams.values():
                sp.cleanup_old_segments()
            await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL)

    async def _check_and_act(self, sp: StreamProcess):
        alive = await sp.check_health()
        prev_mode = sp.mode

        if alive:
            sp.consecutive_failures = 0
            if sp.mode != StreamStatus.LIVE:
                await sp.start_live()
                await sp.start_dvr_recording()
                await self._log(sp.stream_id, "live", "Source came online — switched to LIVE")
                await self._broadcast(sp)
        else:
            sp.consecutive_failures += 1
            if sp.consecutive_failures >= settings.HEALTH_CHECK_FAILURES_BEFORE_DOWN and sp.mode == StreamStatus.LIVE:
                logger.warning(f"[{sp.name}] Source DOWN after {sp.consecutive_failures} failures — switching to DVR")
                await sp.start_dvr_playback()
                await self._log(sp.stream_id, "dvr", "Source went offline — switched to DVR playback")
                await self._broadcast(sp)

        # persist status in DB
        async with async_session() as db:
            await db.execute(
                update(Stream)
                .where(Stream.id == sp.stream_id)
                .values(
                    status=sp.mode,
                    consecutive_failures=sp.consecutive_failures,
                    last_checked=datetime.now(timezone.utc),
                    last_online=sp.last_online,
                )
            )
            await db.commit()

    # ── logging + websocket ───────────────────────────────────────────────
    async def _log(self, stream_id: int, event: str, message: str):
        logger.info(f"Stream {stream_id}: [{event}] {message}")
        async with async_session() as db:
            db.add(StreamLog(stream_id=stream_id, event=event, message=message))
            await db.commit()

    async def _broadcast(self, sp: StreamProcess):
        data = {"stream_id": sp.stream_id, "name": sp.name, "status": sp.mode.value}
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)

    def register_ws(self, ws):
        self._ws_clients.append(ws)

    def unregister_ws(self, ws):
        try:
            self._ws_clients.remove(ws)
        except ValueError:
            pass

    # ── status for API ────────────────────────────────────────────────────
    def get_all_status(self) -> list[dict]:
        return [
            {
                "stream_id": sp.stream_id,
                "name": sp.name,
                "status": sp.mode.value,
                "consecutive_failures": sp.consecutive_failures,
                "last_online": sp.last_online.isoformat() if sp.last_online else None,
                "dvr_segments": len(sp._get_recent_segments()),
            }
            for sp in self.streams.values()
        ]


engine = Engine()
