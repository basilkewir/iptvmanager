"""
Stream Engine — manages health checks, FFmpeg processes, DVR recording,
live→DVR failover, and UDP multicast output.

Architecture per stream:
  - One FFmpeg process always outputs to UDP multicast (live OR dvr)
  - One FFmpeg process records .ts segments while source is live
  - Health checker runs every N seconds
  - On source failure → kill live output, start DVR loop output (same UDP)
  - On source recovery → kill DVR output, start live output (same UDP)
"""
import asyncio
import logging
import os
import time
import glob
from datetime import datetime, timezone
from typing import Dict, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import Stream, StreamStatus, StreamLog

logger = logging.getLogger("engine")


class StreamProcess:
    """Wraps FFmpeg subprocesses + state for one stream channel."""

    def __init__(self, stream_id: int, name: str, source_url: str,
                 rtmp_key: str, dvr_hours: int, udp_target: str,
                 logo_path: str = None, logo_x: int = 10, logo_y: int = 10):
        self.stream_id = stream_id
        self.name = name
        self.source_url = source_url
        self.rtmp_key = rtmp_key
        self.dvr_hours = dvr_hours
        self.udp_target = udp_target
        self.logo_path = logo_path
        self.logo_x = logo_x
        self.logo_y = logo_y
        self.output_process: Optional[asyncio.subprocess.Process] = None
        self.recorder_process: Optional[asyncio.subprocess.Process] = None
        self.mode: StreamStatus = StreamStatus.STOPPED
        self.manually_stopped: bool = False      # set True by manual stop; suppresses auto-restart
        self.consecutive_failures = 0
        self.last_online: Optional[datetime] = None
        self.dvr_started_at: Optional[datetime] = None   # when DVR playback began
        self.lock = asyncio.Lock()

    @property
    def dvr_dir(self) -> str:
        d = os.path.join(settings.DVR_STORAGE_PATH, self.rtmp_key)
        os.makedirs(d, exist_ok=True)
        return d

    # ── health check ─────────────────────────────────────────────────────
    async def check_health(self) -> bool:
        """Return True if source stream is alive and producing media."""
        try:
            cmd = [settings.FFPROBE_PATH, "-v", "error"]
            if self.source_url.lower().startswith("rtsp://"):
                cmd += ["-rtsp_transport", "tcp"]
            cmd += [
                "-analyzeduration", "5000000",
                "-probesize", "5000000",
                "-i", self.source_url,
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
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
                logger.info(f"[{self.name}] health check TIMED OUT")
                return False

            alive = proc.returncode == 0 and len(stdout.strip()) > 0
            if alive:
                logger.info(f"[{self.name}] health check OK")
            else:
                stderr_msg = stderr.decode(errors="ignore").strip()[:200]
                logger.info(f"[{self.name}] health check FAILED: rc={proc.returncode} {stderr_msg}")
            return alive
        except Exception as e:
            logger.warning(f"[{self.name}] health check error: {e}")
            return False

    # ── logo overlay helper ────────────────────────────────────────────
    def _has_logo(self) -> bool:
        return bool(self.logo_path) and os.path.isfile(self.logo_path)

    def _overlay_expr(self) -> str:
        """Return FFmpeg overlay position expression using custom X/Y."""
        return f"{self.logo_x}:{self.logo_y}"

    def _build_logo_filter(self) -> tuple[list[str], list[str]]:
        """
        Returns (extra_input_args, filter_and_codec_args) for logo overlay.
        Scales logo to 150px wide (keeps aspect ratio), handles alpha,
        and loops the image so it never runs out of frames.
        -framerate 25 on the logo input ensures the filter graph has a
        consistent frame rate to sync against the live source.
        """
        extra_in = ["-loop", "1", "-framerate", "25", "-i", self.logo_path]
        filt = (
            f"[1:v]format=rgba,scale=150:-1[logo];"
            f"[0:v][logo]overlay={self._overlay_expr()}"
        )
        codec = [
            "-filter_complex", filt,
            "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-b:v", "4000k", "-maxrate", "4500k", "-bufsize", "8000k",
            "-g", "50", "-keyint_min", "25",
            "-c:a", "aac", "-b:a", "128k",
        ]
        return extra_in, codec

    # ── LIVE: source → UDP multicast ─────────────────────────────────────
    async def start_live_output(self):
        """Push live source directly to UDP multicast."""
        async with self.lock:
            await self._kill_output()
            logger.info(f"[{self.name}] Starting LIVE → {self.udp_target}")
            cmd = [settings.FFMPEG_PATH]
            has_logo = self._has_logo()
            if self.source_url.lower().startswith("rtsp://"):
                cmd += ["-rtsp_transport", "tcp"]
            else:
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                        "-reconnect_delay_max", "5"]
            # -re paces input to real-time — only safe in copy mode.
            # When transcoding (logo overlay), libx264 paces itself and
            # -re causes frame-timing conflicts with the looped image input.
            if not has_logo:
                cmd += ["-re"]
            cmd += [
                "-fflags", "+genpts+discardcorrupt",
                "-analyzeduration", "2000000",
                "-probesize", "2000000",
                "-i", self.source_url,
            ]
            if has_logo:
                extra_in, codec_args = self._build_logo_filter()
                cmd += extra_in + codec_args
            else:
                cmd += ["-c", "copy"]
            cmd += [
                "-f", "mpegts",
                "-mpegts_flags", "+resend_headers",
                "-pcr_period", "20",
                self.udp_target,
            ]
            logger.info(f"[{self.name}] CMD: {' '.join(cmd)}")
            self.output_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            asyncio.create_task(self._log_ffmpeg(self.output_process, "live-out"))
            self.mode = StreamStatus.LIVE
            self.consecutive_failures = 0
            self.last_online = datetime.now(timezone.utc)

    # ── DVR RECORDER: source → .ts segments (only while live) ────────────
    async def start_dvr_recording(self):
        """Record live source to local .ts segment files."""
        async with self.lock:
            await self._kill_recorder()
            logger.info(f"[{self.name}] Starting DVR recording → {self.dvr_dir}")
            seg_path = os.path.join(self.dvr_dir, "seg_%05d.ts")
            cmd = [settings.FFMPEG_PATH]
            if self.source_url.lower().startswith("rtsp://"):
                cmd += ["-rtsp_transport", "tcp"]
            else:
                # Auto-reconnect for HTTP/HLS sources — recorder survives brief glitches
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                        "-reconnect_delay_max", "5"]
            cmd += [
                "-fflags", "+genpts+discardcorrupt",
                "-i", self.source_url,
                "-c", "copy",
                "-f", "segment",
                "-segment_time", str(settings.DVR_SEGMENT_DURATION),
                "-segment_format", "mpegts",
                "-reset_timestamps", "1",
                "-break_non_keyframes", "0",
                seg_path,
            ]
            self.recorder_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            asyncio.create_task(self._log_ffmpeg(self.recorder_process, "dvr-rec"))

    async def stop_dvr_recording(self):
        async with self.lock:
            await self._kill_recorder()
            logger.info(f"[{self.name}] DVR recording stopped")

    # ── DVR PLAYBACK: .ts segments → UDP multicast (failover) ────────────
    async def start_dvr_playback(self):
        """Loop recent DVR segments to UDP multicast."""
        async with self.lock:
            await self._kill_output()
            await self._kill_recorder()
            segments = self._get_recent_segments()
            if not segments:
                logger.warning(f"[{self.name}] No DVR segments — output goes dark")
                self.mode = StreamStatus.DOWN
                return

            # Write a snapshot playlist — only include segments that exist RIGHT NOW
            # so FFmpeg doesn't crash when cleanup later removes old ones
            concat_path = os.path.join(self.dvr_dir, "playlist.txt")
            # Keep only the most recent 30 min of segments for the loop to stay fresh
            keep_secs = min(self.dvr_hours * 3600, 1800)
            cutoff = time.time() - keep_secs
            playlist_segs = [s for s in segments if os.path.getmtime(s) >= cutoff] or segments
            with open(concat_path, "w") as f:
                for seg in playlist_segs:
                    f.write(f"file '{seg}'\n")

            logger.info(
                f"[{self.name}] Starting DVR playback "
                f"({len(playlist_segs)} segments, {keep_secs//60:.0f} min window)"
                f" → {self.udp_target}"
            )
            cmd = [
                settings.FFMPEG_PATH,
                "-re",
                "-fflags", "+genpts+igndts",
                "-stream_loop", "-1",
                "-f", "concat", "-safe", "0",
                "-i", concat_path,
            ]
            if self._has_logo():
                extra_in, codec_args = self._build_logo_filter()
                cmd += extra_in + codec_args
            else:
                cmd += ["-c", "copy"]
            cmd += [
                "-f", "mpegts",
                "-mpegts_flags", "+resend_headers",
                "-pcr_period", "20",
                self.udp_target,
            ]
            self.output_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            asyncio.create_task(self._log_ffmpeg(self.output_process, "dvr-play"))
            self.mode = StreamStatus.DVR
            self.dvr_started_at = datetime.now(timezone.utc)

    # ── stop all ─────────────────────────────────────────────────────────
    async def stop(self):
        async with self.lock:
            await self._kill_output()
            await self._kill_recorder()
            self.mode = StreamStatus.STOPPED
            self.manually_stopped = True

    # ── internal helpers ─────────────────────────────────────────────────
    async def _kill_output(self):
        if self.output_process and self.output_process.returncode is None:
            try:
                self.output_process.terminate()
                await asyncio.wait_for(self.output_process.wait(), timeout=5)
            except Exception:
                try:
                    self.output_process.kill()
                except Exception:
                    pass
            self.output_process = None

    async def _kill_recorder(self):
        if self.recorder_process and self.recorder_process.returncode is None:
            try:
                self.recorder_process.terminate()
                await asyncio.wait_for(self.recorder_process.wait(), timeout=5)
            except Exception:
                try:
                    self.recorder_process.kill()
                except Exception:
                    pass
            self.recorder_process = None

    async def _log_ffmpeg(self, proc: asyncio.subprocess.Process, label: str):
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                msg = line.decode(errors="ignore").strip()
                if not msg:
                    continue
                # Surface errors and warnings prominently; suppress verbose info
                low = msg.lower()
                if any(k in low for k in ("error", "invalid", "failed", "no such", "unable")):
                    logger.warning(f"[{self.name}] ffmpeg({label}): {msg}")
                else:
                    logger.debug(f"[{self.name}] ffmpeg({label}): {msg}")
        except Exception:
            pass

    def _get_recent_segments(self) -> list[str]:
        cutoff = time.time() - (self.dvr_hours * 3600)
        files = sorted(
            glob.glob(os.path.join(self.dvr_dir, "seg_*.ts")),
            key=os.path.getmtime,
        )
        return [f for f in files if os.path.getmtime(f) >= cutoff]

    def cleanup_old_segments(self):
        # Don't delete segments while DVR playback is active —
        # FFmpeg holds open file handles to the playlist files and will crash
        # if any disappear. Cleanup resumes once we're back LIVE.
        if self.mode == StreamStatus.DVR:
            return
        cutoff = time.time() - (self.dvr_hours * 3600)
        removed = 0
        for f in glob.glob(os.path.join(self.dvr_dir, "seg_*.ts")):
            if os.path.getmtime(f) < cutoff:
                try:
                    os.remove(f)
                    removed += 1
                except Exception:
                    pass
        if removed:
            logger.debug(f"[{self.name}] cleaned up {removed} old DVR segments")


# ═══════════════════════════════════════════════════════════════════════════
class Engine:
    def __init__(self):
        self.streams: Dict[int, StreamProcess] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ws_clients: list = []

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self):
        if self._running:
            return
        self._running = True
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

    # ── stream management ─────────────────────────────────────────────────
    def _make_udp_target(self, stream: Stream) -> str:
        port = settings.UDP_MULTICAST_PORT_START + stream.id
        return (
            f"{settings.UDP_MULTICAST_BASE}:{port}"
            f"?pkt_size=1316"
            f"&buffer_size=4194304"
            f"&ttl={settings.UDP_TTL}"
            f"&overrun_nonfatal=1"
        )

    def _register(self, s: Stream) -> StreamProcess:
        udp = self._make_udp_target(s)
        sp = StreamProcess(
            s.id, s.name, s.source_url, s.rtmp_key, s.dvr_hours, udp,
            logo_path=s.logo_path,
            logo_x=s.logo_x if s.logo_x is not None else 10,
            logo_y=s.logo_y if s.logo_y is not None else 10,
        )
        self.streams[s.id] = sp
        logger.info(f"Registered stream [{s.name}] → {udp}")
        return sp

    async def add_stream(self, s: Stream):
        sp = self._register(s)
        await self._check_and_act(sp)

    async def remove_stream(self, stream_id: int):
        sp = self.streams.pop(stream_id, None)
        if sp:
            await sp.stop()

    async def stop_stream(self, stream_id: int):
        """Stop a stream's FFmpeg processes without removing it from the engine."""
        sp = self.streams.get(stream_id)
        if sp:
            await sp.stop()   # sets manually_stopped = True
            await self._log(stream_id, "stopped", "Stream manually stopped")
            await self._broadcast(sp)

    async def start_stream(self, stream_id: int):
        """Force-start a stopped/down stream immediately (skip health check delay)."""
        sp = self.streams.get(stream_id)
        if sp:
            sp.manually_stopped = False   # allow auto-restart again
            sp.consecutive_failures = 0
            await self._check_and_act(sp)
            await self._broadcast(sp)

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
            for sp in self.streams.values():
                sp.cleanup_old_segments()
            await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL)

    async def _check_and_act(self, sp: StreamProcess):
        # Don't touch manually stopped streams — user must press Start
        if sp.manually_stopped:
            return

        alive = await sp.check_health()
        logger.info(
            f"[{sp.name}] Health: alive={alive}, mode={sp.mode.value}, "
            f"failures={sp.consecutive_failures}, udp={sp.udp_target}"
        )

        if alive:
            sp.consecutive_failures = 0
            if sp.mode != StreamStatus.LIVE:
                # Source is up → switch to live + start recording
                await sp.start_live_output()
                await sp.start_dvr_recording()
                await self._log(sp.stream_id, "live",
                                "Source online — streaming LIVE to UDP multicast")
                await self._broadcast(sp)
            else:
                # Already LIVE — make sure output & recorder are still running
                if sp.output_process and sp.output_process.returncode is not None:
                    logger.warning(f"[{sp.name}] Live output process died — restarting")
                    await sp.start_live_output()
                if sp.recorder_process and sp.recorder_process.returncode is not None:
                    logger.warning(f"[{sp.name}] DVR recorder process died — restarting")
                    await sp.start_dvr_recording()
        else:
            sp.consecutive_failures += 1
            threshold = settings.HEALTH_CHECK_FAILURES_BEFORE_DOWN
            if sp.consecutive_failures >= threshold and sp.mode in (
                StreamStatus.LIVE, StreamStatus.STOPPED
            ):
                # Source died or was never up → switch to DVR playback
                logger.warning(
                    f"[{sp.name}] Source DOWN ({sp.consecutive_failures} failures) "
                    f"— switching to DVR playback"
                )
                await sp.start_dvr_playback()
                await self._log(sp.stream_id, "dvr",
                                "Source offline — playing DVR to UDP multicast")
                await self._broadcast(sp)
            elif sp.mode == StreamStatus.DVR:
                # Already in DVR playback while source is still down.
                # Restart DVR if: process died OR playlist is >30 min old
                # (refresh picks up newest segments and avoids deleted-file crashes)
                playlist_age_secs = (
                    (datetime.now(timezone.utc) - sp.dvr_started_at).total_seconds()
                    if sp.dvr_started_at else 99999
                )
                process_dead = (sp.output_process is None or
                                sp.output_process.returncode is not None)
                if process_dead or playlist_age_secs >= 1800:
                    reason = "process died" if process_dead else "30-min playlist refresh"
                    logger.warning(f"[{sp.name}] DVR playback restarting ({reason})")
                    await sp.start_dvr_playback()
                    if sp.mode == StreamStatus.DOWN:
                        await self._log(sp.stream_id, "down",
                                        "DVR playback failed — no segments available")
                        await self._broadcast(sp)
            elif sp.mode == StreamStatus.DOWN:
                # No segments last time — retry DVR in case old segments exist
                if sp._get_recent_segments():
                    logger.info(f"[{sp.name}] DVR segments found — retrying playback")
                    await sp.start_dvr_playback()
                    if sp.mode == StreamStatus.DVR:
                        await self._log(sp.stream_id, "dvr",
                                        "DVR segments available — playing to UDP multicast")
                        await self._broadcast(sp)

        # Persist to DB
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
        data = {
            "stream_id": sp.stream_id,
            "name": sp.name,
            "status": sp.mode.value,
            "udp_target": sp.udp_target,
        }
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
                "udp_target": sp.udp_target,
                "consecutive_failures": sp.consecutive_failures,
                "last_online": sp.last_online.isoformat() if sp.last_online else None,
                "dvr_segments": len(sp._get_recent_segments()),
            }
            for sp in self.streams.values()
        ]

    def get_stream_dvr_detail(self, stream_id: int) -> dict | None:
        sp = self.streams.get(stream_id)
        if not sp:
            return None
        files = sorted(
            glob.glob(os.path.join(sp.dvr_dir, "seg_*.ts")),
            key=os.path.getmtime,
        )
        segments = []
        total_size = 0
        for f in files:
            try:
                sz = os.path.getsize(f)
                mt = os.path.getmtime(f)
                total_size += sz
                segments.append({
                    "name": os.path.basename(f),
                    "size": sz,
                    "modified": datetime.fromtimestamp(mt).isoformat(),
                })
            except OSError:
                continue
        return {
            "stream_id": stream_id,
            "name": sp.name,
            "rtmp_key": sp.rtmp_key,
            "segment_count": len(segments),
            "total_size": total_size,
            "oldest": segments[0]["modified"] if segments else None,
            "newest": segments[-1]["modified"] if segments else None,
            "segments": segments,
        }

    def get_dvr_summary(self) -> list[dict]:
        results = []
        for sp in self.streams.values():
            files = glob.glob(os.path.join(sp.dvr_dir, "seg_*.ts"))
            total_size = 0
            oldest_t = None
            newest_t = None
            for f in files:
                try:
                    sz = os.path.getsize(f)
                    mt = os.path.getmtime(f)
                    total_size += sz
                    if oldest_t is None or mt < oldest_t:
                        oldest_t = mt
                    if newest_t is None or mt > newest_t:
                        newest_t = mt
                except OSError:
                    continue
            results.append({
                "stream_id": sp.stream_id,
                "name": sp.name,
                "rtmp_key": sp.rtmp_key,
                "segment_count": len(files),
                "total_size": total_size,
                "oldest": datetime.fromtimestamp(oldest_t).isoformat() if oldest_t else None,
                "newest": datetime.fromtimestamp(newest_t).isoformat() if newest_t else None,
            })
        return results


engine = Engine()
