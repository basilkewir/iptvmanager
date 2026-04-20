# IPTV Manager

A self-hosted IPTV stream controller with automatic live/DVR failover, RTMP push to Flussonic, and a web dashboard with authentication.

## What It Does

```
Source Stream (HLS/RTSP) → YOUR ENGINE → RTMP → Flussonic → DVR / Multicast / Delivery
```

- **LIVE**: Monitors your source stream, pushes it to Flussonic via RTMP, records DVR segments locally
- **FAILOVER**: When source goes down, automatically switches to DVR playback (loops recent recordings) → Flussonic never loses the feed
- **RECOVERY**: When source comes back, instantly switches back to live and resumes recording
- **NO OFFLINE RECORDING**: DVR recording stops immediately when source is down — no garbage segments

## Requirements

- **Ubuntu Server** (18.04+)
- **Python 3.10+**
- **FFmpeg** (with ffprobe)
- **Flussonic** Media Server (configured to accept RTMP on `rtmp://localhost/live`)

## Quick Start

```bash
# 1. Clone / upload to server
cd /opt/iptvmanager

# 2. Run setup (installs deps, creates venv, generates config)
chmod +x setup.sh
./setup.sh

# 3. Start
source venv/bin/activate
python run.py
```

Open `http://YOUR_IP:8000` — register a user on first visit.

## Configuration

Edit `.env` (auto-generated from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `APP_PORT` | `8000` | Web UI port |
| `SECRET_KEY` | auto-generated | JWT signing key |
| `FLUSSONIC_RTMP_BASE` | `rtmp://localhost/live` | Where to push RTMP |
| `HEALTH_CHECK_INTERVAL` | `5` | Seconds between health checks |
| `HEALTH_CHECK_FAILURES_BEFORE_DOWN` | `3` | Failures before switching to DVR |
| `DVR_RETENTION_HOURS` | `24` | How long to keep DVR segments |
| `DVR_SEGMENT_DURATION` | `10` | Seconds per .ts segment |

## Architecture

```
┌──────────────────────────────────────────────────┐
│                  Web UI (:8000)                   │
│  Login → Dashboard → Add/Edit/Delete Streams     │
│  Real-time status via WebSocket                  │
└──────────────┬───────────────────────────────────┘
               │ REST API + JWT Auth
┌──────────────▼───────────────────────────────────┐
│              Stream Engine                        │
│  • Health checker (ffprobe every 5s)              │
│  • FFmpeg live push (source → RTMP)               │
│  • FFmpeg DVR recorder (source → .ts segments)    │
│  • FFmpeg DVR player (segments → RTMP on failure) │
│  • Auto cleanup of old DVR segments               │
└──────────────┬───────────────────────────────────┘
               │ RTMP
┌──────────────▼───────────────────────────────────┐
│           Flussonic Media Server                  │
│  • DVR recording                                  │
│  • UDP multicast                                  │
│  • HLS/DASH delivery                              │
└──────────────────────────────────────────────────┘
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/register` | Create account |
| POST | `/api/auth/login` | Get JWT token |
| GET | `/api/auth/me` | Current user info |
| GET | `/api/streams/` | List all streams |
| POST | `/api/streams/` | Add a stream |
| PUT | `/api/streams/{id}` | Update a stream |
| DELETE | `/api/streams/{id}` | Remove a stream |
| GET | `/api/streams/status` | Live engine status |
| GET | `/api/streams/{id}/logs` | Stream event logs |
| WS | `/ws/status` | Real-time status updates |

## Running as a Service (systemd)

```bash
sudo tee /etc/systemd/system/iptvmanager.service << 'EOF'
[Unit]
Description=IPTV Manager
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/iptvmanager
ExecStart=/opt/iptvmanager/venv/bin/python run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now iptvmanager
```

## License

MIT
