# IPTV Manager

A self-hosted IPTV stream controller — health checks, live→DVR failover, UDP multicast output, logo overlay, and a real-time web dashboard.

## What It Does

```
Source Stream (HLS/RTSP/HTTP) → Engine → UDP Multicast (239.0.0.1:500X)
                                       → DVR Segments (.ts files, looped on failure)
```

- **LIVE**: Monitors source streams via ffprobe, outputs to UDP multicast
- **DVR RECORDING**: Continuously records to timestamped .ts segments while source is live
- **FAILOVER**: Source goes down → engine immediately loops recent DVR segments to the same UDP output — viewers see no interruption
- **RECOVERY**: Source comes back → instantly switches back to live, resumes recording
- **LOGO OVERLAY**: Per-stream PNG/JPG watermark, positioned by percentage (resolution-independent)
- **HEALTH ENDPOINT**: `GET /api/health` — no auth, for external monitoring

## Quick Install (fresh server)

**See [INSTALL.md](INSTALL.md) for the full step-by-step guide.**

Short version:
```bash
sudo apt install -y python3 python3-venv ffmpeg git
sudo mkdir -p /opt/iptvmanager && sudo chown $USER /opt/iptvmanager
cd /opt/iptvmanager
git clone https://github.com/basilkewir/iptvmanager.git .
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit SECRET_KEY at minimum
python run.py
```

Open `http://YOUR_IP:8000` — register your admin account on first visit.

## Updating

```bash
bash /opt/iptvmanager/deploy.sh
```

Pulls latest code, migrates DB, restarts service. Safe — never touches your data.

## Configuration

Edit `.env`:

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | — | **Required.** JWT signing key — generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `APP_PORT` | `8000` | Web UI port |
| `HEALTH_CHECK_INTERVAL` | `5` | Seconds between source health checks |
| `HEALTH_CHECK_FAILURES_BEFORE_DOWN` | `2` | Consecutive failures before DVR failover |
| `DVR_RETENTION_HOURS` | `2` | How long to keep .ts segments |
| `DVR_SEGMENT_DURATION` | `6` | Seconds per segment file |
| `UDP_MULTICAST_BASE` | `udp://239.0.0.1` | Multicast group base address |
| `UDP_MULTICAST_PORT_START` | `5000` | First stream uses 5001, second 5002, etc. |

## Architecture

```
┌───────────────────────────────────────────────────┐
│               Web UI (:8000)                       │
│  Dashboard · Streams · DVR Browser · Settings     │
│  Real-time status via WebSocket                   │
└────────────────┬──────────────────────────────────┘
                 │ REST API + JWT Auth
┌────────────────▼──────────────────────────────────┐
│              Stream Engine (engine.py)             │
│  • ffprobe health check every 5 s (per stream)    │
│  • FFmpeg live output  → UDP multicast            │
│  • FFmpeg DVR recorder → timestamped .ts files    │
│  • FFmpeg DVR playback → same UDP on failover     │
│  • Logo overlay via libx264 (resolution-aware)    │
│  • Disk space guard, segment cleanup              │
└────────────────┬──────────────────────────────────┘
                 │ UDP multicast 239.0.0.1:500X
        IPTV receivers / Flussonic / VLC
```

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/auth/register` | No | Create account |
| `POST` | `/api/auth/login` | No | Get JWT token |
| `GET` | `/api/health` | No | Liveness check + stream summary |
| `GET` | `/api/streams/` | Yes | List all streams with live stats |
| `POST` | `/api/streams/` | Yes | Add a stream |
| `PUT` | `/api/streams/{id}` | Yes | Update a stream |
| `DELETE` | `/api/streams/{id}` | Yes | Remove a stream |
| `POST` | `/api/streams/{id}/start` | Yes | Force-start a stream |
| `POST` | `/api/streams/{id}/stop` | Yes | Stop a stream |
| `POST` | `/api/streams/{id}/logo` | Yes | Upload or set logo URL |
| `DELETE` | `/api/streams/{id}/logo` | Yes | Remove logo |
| `GET` | `/api/streams/{id}/logs` | Yes | Event log for a stream |
| `GET` | `/api/streams/dvr/summary` | Yes | DVR storage summary |
| `GET` | `/api/streams/{id}/dvr` | Yes | DVR segments for one stream |
| `WS` | `/ws/status` | No | Real-time status push |

## License

MIT


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
