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
        self.rtmp_target = f"{settings.RTMP_SERVER_URL}/{self.rtmp_key}" if self.rtmp_key else None

        # HLS output directory for this stream
        self.hls_dir = os.path.join(settings.HLS_OUTPUT_DIR, str(stream_id))
        os.makedirs(self.hls_dir, exist_ok=True)
        self.hls_target = os.path.join(self.hls_dir, "index.m3u8")

        self.logo_path = logo_path
        self.logo_x = logo_x
        self.logo_y = logo_y

        # FFmpeg processes — three separate processes for full isolation
        self.output_process: Optional[asyncio.subprocess.Process] = None   # UDP + HLS
        self.rtmp_process: Optional[asyncio.subprocess.Process] = None      # RTMP relay
        self.recorder_process: Optional[asyncio.subprocess.Process] = None  # DVR recorder

        self.mode: StreamStatus = StreamStatus.STOPPED
        self.manually_stopped: bool = False
        self.consecutive_failures = 0
        self.last_online: Optional[datetime] = None
        self.dvr_started_at: Optional[datetime] = None
        # Cached video resolution — probed once on first live start, reused for DVR
        self._video_width: int = 1920
        self._video_height: int = 1080
        self.lock = asyncio.Lock()
        self.check_lock = asyncio.Lock()
        # RTMP exponential backoff — avoids hammering a dead RTMP server
        self._rtmp_fail_count: int = 0
        self._rtmp_next_retry: float = 0.0  # epoch seconds

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

    # ── logo overlay helpers ───────────────────────────────────────────
    def _has_logo(self) -> bool:
        return bool(self.logo_path) and os.path.isfile(self.logo_path)

    async def _probe_video_size(self) -> tuple[int, int]:
        """
        Probe the source stream and return (width, height).
        Result is cached on self._video_width / self._video_height so we
        only hit the network once per stream lifecycle — DVR playback reuses
        the last known resolution.
        Falls back to 1920×1080 if probing fails.
        """
        try:
            cmd = [settings.FFPROBE_PATH, "-v", "error"]
            if self.source_url.lower().startswith("rtsp://"):
                cmd += ["-rtsp_transport", "tcp"]
            cmd += [
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
                "-i", self.source_url,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                logger.warning(f"[{self.name}] video size probe timed out — using {self._video_width}×{self._video_height}")
                return self._video_width, self._video_height

            parts = stdout.decode().strip().split(",")
            if len(parts) == 2:
                w, h = int(parts[0]), int(parts[1])
                self._video_width, self._video_height = w, h
                logger.info(f"[{self.name}] video size: {w}×{h}")
                return w, h
        except Exception as e:
            logger.warning(f"[{self.name}] video size probe failed: {e} — using {self._video_width}×{self._video_height}")
        return self._video_width, self._video_height

    def _build_logo_filter(self, video_w: int, video_h: int) -> tuple[list[str], list[str]]:
        """
        Build FFmpeg logo overlay filter using PERCENTAGE-based X/Y positions.

        logo_x / logo_y are 0–100 (percent of video width/height).
        The logo is scaled to 10 % of the video width (min 40 px).
        This guarantees the logo is always visible regardless of resolution —
        SD (480p), HD (720p/1080p) or 4K all produce correct results.

        Example: 1920×1080, logo_x=5, logo_y=5
          → logo width = max(192, 40) = 192 px
          → x_px = int(1920 * 5/100) = 96
          → y_px = int(1080 * 5/100) = 54
        """
        logo_w_px = max(int(video_w * 0.10), 40)
        x_px = int(video_w * self.logo_x / 100)
        y_px = int(video_h * self.logo_y / 100)

        # Clamp so the logo can't overflow the frame
        x_px = min(x_px, video_w - logo_w_px - 4)
        y_px = min(y_px, video_h - 40)
        x_px = max(x_px, 0)
        y_px = max(y_px, 0)

        logger.info(
            f"[{self.name}] logo overlay: {video_w}×{video_h} "
            f"pos=({self.logo_x}%,{self.logo_y}%) → px=({x_px},{y_px}) "
            f"logo_w={logo_w_px}px"
        )

        extra_in = ["-loop", "1", "-framerate", "25", "-i", self.logo_path]
        filt = (
            f"[1:v]format=rgba,scale={logo_w_px}:-1[logo];"
            f"[0:v][logo]overlay={x_px}:{y_px}"
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

    # ── LIVE: source → UDP + HLS (tee) + RTMP (separate process) ──────────
    async def start_live_output(self):
        """Push live source to UDP multicast + HLS + RTMP (separate relay)."""
        async with self.lock:
            await self._kill_output()
            await self._kill_rtmp_relay()

            logger.info(f"[{self.name}] Starting LIVE → {self.udp_target}")
            has_logo = self._has_logo()
            if has_logo:
                vid_w, vid_h = await self._probe_video_size()

            cmd = [settings.FFMPEG_PATH]
            if self.source_url.lower().startswith("rtsp://"):
                cmd += ["-rtsp_transport", "tcp"]
            else:
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                        "-reconnect_delay_max", "5"]
            cmd += [
                "-fflags", "+genpts+discardcorrupt",
                "-analyzeduration", "2000000",
                "-probesize", "2000000",
                "-i", self.source_url,
            ]

            if has_logo:
                extra_in, codec_args = self._build_logo_filter(vid_w, vid_h)
                cmd += extra_in + codec_args
            else:
                cmd += ["-c", "copy", "-map", "0"]

            # Tee muxer: UDP multicast + HLS simultaneously
            tee_outputs = [
                f"[f=mpegts:mpegts_flags=+resend_headers:pcr_period=20]{self.udp_target}",
                (f"[f=hls:hls_time=4:hls_list_size=6"
                 f":hls_flags=delete_segments+append_list+temp_file"
                 f":hls_segment_filename={self.hls_dir}/seg%05d.ts]{self.hls_target}"),
            ]
            cmd += ["-f", "tee", "|".join(tee_outputs)]

            logger.info(f"[{self.name}] LIVE CMD: {' '.join(cmd)}")
            self.output_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            asyncio.create_task(self._log_ffmpeg(self.output_process, "live-out"))

            # Start RTMP relay as an independent process (reconnects on its own)
            if self.rtmp_target:
                await self._start_rtmp_relay(self.source_url)

            self.mode = StreamStatus.LIVE
            self.consecutive_failures = 0
            self.last_online = datetime.now(timezone.utc)

    async def _start_rtmp_relay(self, source_url: str):
        """Push stream to RTMP server as a separate resilient process.
        Uses exponential backoff: 10s → 20s → 40s → ... → max 300s between retries.
        """
        if not self.rtmp_target:
            return
        now = time.time()
        if now < self._rtmp_next_retry:
            wait_secs = int(self._rtmp_next_retry - now)
            logger.debug(f"[{self.name}] RTMP backoff: {wait_secs}s remaining before retry")
            return
        await self._kill_rtmp_relay()
        cmd = [settings.FFMPEG_PATH]
        if source_url.lower().startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp"]
        else:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]
        cmd += [
            "-fflags", "+genpts+discardcorrupt",
            "-analyzeduration", "2000000",
            "-probesize", "2000000",
            "-i", source_url,
            "-c", "copy",
            "-map", "0",
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
            self.rtmp_target,
        ]
        logger.info(f"[{self.name}] RTMP CMD: {' '.join(cmd)}")
        self.rtmp_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._log_ffmpeg_rtmp(self.rtmp_process, "rtmp-relay"))

    # ── DVR RECORDER: source → .ts segments (only while live) ────────────
    async def start_dvr_recording(self):
        """Record live source to local .ts segment files."""
        async with self.lock:
            await self._kill_recorder()

            # Guard against filling the disk
            import shutil as _shutil
            try:
                usage = _shutil.disk_usage(settings.DVR_STORAGE_PATH)
                pct = usage.used / usage.total * 100
                if pct > 90:
                    logger.error(
                        f"[{self.name}] Disk {pct:.0f}% full — DVR recording NOT started"
                    )
                    return
            except Exception:
                pass

            logger.info(f"[{self.name}] Starting DVR recording → {self.dvr_dir}")
            # Use strftime-based names so segments are never overwritten on restart
            seg_path = os.path.join(self.dvr_dir, "seg_%Y%m%d_%H%M%S.ts")
            cmd = [settings.FFMPEG_PATH]
            if self.source_url.lower().startswith("rtsp://"):
                cmd += [
                    "-rtsp_transport", "tcp",
                    "-rtsp_flags", "prefer_tcp",
                    "-timeout", "10000000",   # 10s reconnect timeout
                ]
            else:
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                        "-reconnect_delay_max", "5"]
            cmd += [
                "-fflags", "+genpts+discardcorrupt",
                "-i", self.source_url,
                "-c", "copy",
                "-f", "segment",
                "-strftime", "1",
                "-segment_time", str(settings.DVR_SEGMENT_DURATION),
                "-segment_format", "mpegts",
                "-reset_timestamps", "1",
                seg_path,
            ]
            logger.info(f"[{self.name}] REC CMD: {' '.join(cmd)}")
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

    # ── DVR PLAYBACK: .ts segments → UDP + HLS + RTMP (failover) ────────
    async def start_dvr_playback(self):
        """Loop recent DVR segments to UDP + HLS + RTMP (automatic failover)."""
        async with self.lock:
            await self._kill_output()
            await self._kill_recorder()
            await self._kill_rtmp_relay()

            segments = self._get_recent_segments()
            if not segments:
                logger.warning(f"[{self.name}] No DVR segments available — channel goes dark")
                self.mode = StreamStatus.DOWN
                return

            # Snapshot playlist — only existing segments so FFmpeg doesn't crash
            # if cleanup later removes files still referenced
            concat_path = os.path.join(self.dvr_dir, "playlist.txt")
            keep_secs = min(self.dvr_hours * 3600, 1800)  # max 30 min loop window
            cutoff = time.time() - keep_secs
            playlist_segs = [s for s in segments if os.path.getmtime(s) >= cutoff] or segments
            with open(concat_path, "w") as f:
                for seg in playlist_segs:
                    f.write(f"file '{seg}'\n")

            logger.info(
                f"[{self.name}] Starting DVR failover playback "
                f"({len(playlist_segs)} segments, {keep_secs//60:.0f} min window) → {self.udp_target}"
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
                extra_in, codec_args = self._build_logo_filter(
                    self._video_width, self._video_height
                )
                cmd += extra_in + codec_args
            else:
                cmd += ["-c", "copy", "-map", "0"]

            # Tee muxer: UDP + HLS
            tee_outputs = [
                f"[f=mpegts:mpegts_flags=+resend_headers:pcr_period=20]{self.udp_target}",
                (f"[f=hls:hls_time=4:hls_list_size=6"
                 f":hls_flags=delete_segments+append_list+temp_file"
                 f":hls_segment_filename={self.hls_dir}/seg%05d.ts]{self.hls_target}"),
            ]
            cmd += ["-f", "tee", "|".join(tee_outputs)]

            logger.info(f"[{self.name}] DVR CMD: {' '.join(cmd)}")
            self.output_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            asyncio.create_task(self._log_ffmpeg(self.output_process, "dvr-play"))

            # Also relay DVR to RTMP using concat source
            if self.rtmp_target:
                await self._start_rtmp_relay_from_concat(concat_path)

            self.mode = StreamStatus.DVR
            self.dvr_started_at = datetime.now(timezone.utc)

    async def _start_rtmp_relay_from_concat(self, concat_path: str):
        """Push DVR loop to RTMP as a separate process. Uses same backoff as live relay."""
        if not self.rtmp_target:
            return
        now = time.time()
        if now < self._rtmp_next_retry:
            return
        await self._kill_rtmp_relay()
        cmd = [
            settings.FFMPEG_PATH,
            "-re",
            "-fflags", "+genpts+igndts",
            "-stream_loop", "-1",
            "-f", "concat", "-safe", "0",
            "-i", concat_path,
            "-c", "copy",
            "-map", "0",
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
            self.rtmp_target,
        ]
        logger.info(f"[{self.name}] DVR-RTMP CMD: {' '.join(cmd)}")
        self.rtmp_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._log_ffmpeg_rtmp(self.rtmp_process, "dvr-rtmp"))

    # ── stop all ─────────────────────────────────────────────────────────
    async def stop(self):
        async with self.lock:
            await self._kill_output()
            await self._kill_rtmp_relay()
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

    async def _kill_rtmp_relay(self):
        if self.rtmp_process and self.rtmp_process.returncode is None:
            try:
                self.rtmp_process.terminate()
                await asyncio.wait_for(self.rtmp_process.wait(), timeout=5)
            except Exception:
                try:
                    self.rtmp_process.kill()
                except Exception:
                    pass
            self.rtmp_process = None

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
                if any(k in low for k in ("error", "invalid", "failed", "no such", "unable", "connection refused", "network is unreachable")):
                    logger.error(f"[{self.name}] ffmpeg({label}): {msg}")
                elif any(k in low for k in ("warning", "deprecated")):
                    logger.warning(f"[{self.name}] ffmpeg({label}): {msg}")
                else:
                    logger.debug(f"[{self.name}] ffmpeg({label}): {msg}")
        except Exception:
            pass
        finally:
            rc = proc.returncode
            if rc is not None and rc != 0:
                logger.error(f"[{self.name}] ffmpeg({label}) exited with code {rc}")

    async def _log_ffmpeg_rtmp(self, proc: asyncio.subprocess.Process, label: str):
        """Like _log_ffmpeg but applies exponential backoff on connection failures."""
        connection_refused = False
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                msg = line.decode(errors="ignore").strip()
                if not msg:
                    continue
                low = msg.lower()
                if "connection refused" in low or "network is unreachable" in low:
                    connection_refused = True
                    logger.warning(f"[{self.name}] ffmpeg({label}): {msg}")
                elif any(k in low for k in ("error", "invalid", "failed", "no such", "unable")):
                    logger.error(f"[{self.name}] ffmpeg({label}): {msg}")
                elif any(k in low for k in ("warning", "deprecated")):
                    logger.warning(f"[{self.name}] ffmpeg({label}): {msg}")
                else:
                    logger.debug(f"[{self.name}] ffmpeg({label}): {msg}")
        except Exception:
            pass
        finally:
            rc = proc.returncode
            if rc is not None and rc != 0:
                if connection_refused:
                    # Exponential backoff: 10 → 20 → 40 → 80 → max 300 seconds
                    self._rtmp_fail_count += 1
                    delay = min(10 * (2 ** (self._rtmp_fail_count - 1)), 300)
                    self._rtmp_next_retry = time.time() + delay
                    logger.warning(
                        f"[{self.name}] RTMP server not reachable at {self.rtmp_target} "
                        f"(attempt {self._rtmp_fail_count}) — retrying in {delay}s. "
                        f"Make sure an RTMP server is running on port 1935."
                    )
                else:
                    # Non-connection error — shorter retry
                    self._rtmp_fail_count = max(self._rtmp_fail_count, 1)
                    self._rtmp_next_retry = time.time() + 10
                    logger.error(f"[{self.name}] ffmpeg({label}) exited with code {rc}")
            else:
                # Successful exit — reset backoff
                self._rtmp_fail_count = 0
                self._rtmp_next_retry = 0.0

    def _get_recent_segments(self) -> list[str]:
        cutoff = time.time() - (self.dvr_hours * 3600)
        files = sorted(
            glob.glob(os.path.join(self.dvr_dir, "seg_*.ts")),
            key=os.path.getmtime,
        )
        return [f for f in files if os.path.getmtime(f) >= cutoff]

    def cleanup_old_segments(self, force: bool = False):
        # Don't delete segments while DVR playback is active —
        # FFmpeg holds open file handles to the playlist files and will crash
        # if any disappear. Cleanup resumes once we're back LIVE.
        # Pass force=True on startup to always purge regardless of mode.
        if self.mode == StreamStatus.DVR and not force:
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
        # Kill any FFmpeg processes left over from a previous run of this service.
        # Without this, a service restart leaves orphaned FFmpeg writing to the
        # same UDP ports — the receiver gets a corrupted mix of two streams.
        await self._kill_orphaned_ffmpeg()
        async with async_session() as db:
            result = await db.execute(select(Stream).where(Stream.enabled == True))
            for s in result.scalars().all():
                self._register(s)
        # Purge any old segments accumulated while the service was down
        for sp in self.streams.values():
            sp.cleanup_old_segments(force=True)
        self._task = asyncio.create_task(self._loop())
        logger.info("Engine started")

    async def _kill_orphaned_ffmpeg(self):
        """
        On startup, terminate any ffmpeg/ffprobe processes already running
        under this user account. This prevents port collisions after a service
        crash or restart where the old process wasn't cleaned up gracefully.
        """
        try:
            import subprocess
            result = subprocess.run(
                ["pgrep", "-u", str(os.getuid()), "ffmpeg"],
                capture_output=True, text=True
            )
            pids = result.stdout.strip().split()
            if pids:
                logger.warning(f"Engine startup: killing {len(pids)} orphaned ffmpeg process(es): {pids}")
                for pid in pids:
                    try:
                        os.kill(int(pid), 15)  # SIGTERM
                    except ProcessLookupError:
                        pass
                await asyncio.sleep(2)  # allow graceful exit
                # SIGKILL any that are still alive
                for pid in pids:
                    try:
                        os.kill(int(pid), 9)
                    except ProcessLookupError:
                        pass  # already gone
        except Exception as e:
            logger.warning(f"Could not check for orphaned ffmpeg processes: {e}")

    async def shutdown(self):
        self._running = False
        if self._task:
            self._task.cancel()
        for sp in self.streams.values():
            await sp.stop()
        logger.info("Engine stopped")

    # ── stream management ─────────────────────────────────────────────────
    def _make_udp_target(self, stream: Stream) -> str:
        """Build the FFmpeg UDP output URL.
        
        Supports both multicast (239.x.x.x) and unicast targets.
        When UDP_MULTICAST_INTERFACE is set, binds to that local IP so
        FFmpeg sends packets out the correct NIC (same as Flussonic localaddr).
        """
        port = settings.UDP_MULTICAST_PORT_START + stream.id
        base = settings.UDP_MULTICAST_BASE  # e.g. udp://239.0.0.1 or udp://192.168.1.123
        is_multicast = any(
            base.replace("udp://", "").startswith(prefix)
            for prefix in ("224.", "225.", "226.", "227.", "228.", "229.",
                           "230.", "231.", "232.", "233.", "234.", "235.",
                           "236.", "237.", "238.", "239.")
        )
        params = [
            f"pkt_size=1316",
            f"buffer_size=4194304",
            f"overrun_nonfatal=1",
            f"fifo_size=50000",
            f"bitrate=2000000"
        ]
        if is_multicast:
            params.append(f"ttl={settings.UDP_TTL}")
            
        if settings.UDP_MULTICAST_INTERFACE:
            params.append(f"localaddr={settings.UDP_MULTICAST_INTERFACE}")
            
        return f"{base}:{port}?" + "&".join(params)

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
        # Run an immediate first check so streams start without waiting for the
        # first sleep interval — critical for fast service restarts.
        tasks = [self._check_and_act(sp) for sp in list(self.streams.values())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        while self._running:
            await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL)
            tasks = [self._check_and_act(sp) for sp in list(self.streams.values())]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for sp in self.streams.values():
                sp.cleanup_old_segments()

    async def _check_and_act(self, sp: StreamProcess):
        # Prevent two concurrent health-check cycles on the same stream.
        # This stops the race between the main loop and a manual start_stream()
        # API call — without this both see mode!=LIVE and double-start FFmpeg.
        if sp.check_lock.locked():
            logger.debug(f"[{sp.name}] check already in progress — skipping")
            return
        async with sp.check_lock:
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
                    await sp.start_live_output()
                    await sp.start_dvr_recording()
                    await self._log(sp.stream_id, "live",
                                    "Source online — streaming LIVE to UDP + HLS + RTMP")
                    await self._broadcast(sp)
                else:
                    # Restart dead sub-processes individually without full restart
                    if sp.output_process and sp.output_process.returncode is not None:
                        logger.warning(f"[{sp.name}] Live output (UDP/HLS) died — restarting")
                        await sp.start_live_output()
                    else:
                        # Check RTMP relay separately — restart without touching UDP/HLS
                        if sp.rtmp_target and (sp.rtmp_process is None or
                                               sp.rtmp_process.returncode is not None):
                            logger.warning(f"[{sp.name}] RTMP relay died — restarting relay only")
                            await sp._start_rtmp_relay(sp.source_url)
                        if sp.recorder_process and sp.recorder_process.returncode is not None:
                            logger.warning(f"[{sp.name}] DVR recorder died — restarting")
                            await sp.start_dvr_recording()
            else:
                sp.consecutive_failures += 1
                threshold = settings.HEALTH_CHECK_FAILURES_BEFORE_DOWN
                if sp.consecutive_failures >= threshold and sp.mode in (
                    StreamStatus.LIVE, StreamStatus.STOPPED
                ):
                    logger.warning(
                        f"[{sp.name}] Source DOWN ({sp.consecutive_failures} failures) "
                        f"— switching to DVR failover playback"
                    )
                    await sp.start_dvr_playback()
                    await self._log(sp.stream_id, "dvr",
                                    "Source offline — DVR failover active on UDP + HLS + RTMP")
                    await self._broadcast(sp)
                elif sp.mode == StreamStatus.DVR:
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
                    else:
                        # Check RTMP relay during DVR — restart if it died
                        if sp.rtmp_target and (sp.rtmp_process is None or
                                               sp.rtmp_process.returncode is not None):
                            concat_path = os.path.join(sp.dvr_dir, "playlist.txt")
                            if os.path.exists(concat_path):
                                logger.warning(f"[{sp.name}] DVR RTMP relay died — restarting")
                                await sp._start_rtmp_relay_from_concat(concat_path)
                elif sp.mode == StreamStatus.DOWN:
                    if sp._get_recent_segments():
                        logger.info(f"[{sp.name}] DVR segments found — retrying playback")
                        await sp.start_dvr_playback()
                        if sp.mode == StreamStatus.DVR:
                            await self._log(sp.stream_id, "dvr",
                                            "DVR segments available — playing to UDP multicast")
                            await self._broadcast(sp)

            # Persist status to DB
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
        segs = sp._get_recent_segments()
        size_mb = round(sum(os.path.getsize(f) for f in segs if os.path.exists(f)) / (1024 * 1024), 2)
        data = {
            "stream_id": sp.stream_id,
            "name": sp.name,
            "status": sp.mode.value,
            "udp_target": sp.udp_target,
            "dvr_segments": len(segs),
            "dvr_size_mb": size_mb,
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
        out = []
        for sp in self.streams.values():
            segs = sp._get_recent_segments()
            size_mb = round(
                sum(os.path.getsize(f) for f in segs if os.path.exists(f)) / (1024 * 1024), 2
            )
            out.append({
                "stream_id": sp.stream_id,
                "name": sp.name,
                "status": sp.mode.value,
                "udp_target": sp.udp_target,
                "consecutive_failures": sp.consecutive_failures,
                "last_online": sp.last_online.isoformat() if sp.last_online else None,
                "dvr_segments": len(segs),
                "dvr_size_mb": size_mb,
                "recorder_running": (
                    sp.recorder_process is not None
                    and sp.recorder_process.returncode is None
                ),
            })
        return out

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
