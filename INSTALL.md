# IPTV Manager — Fresh Server Install Guide

## What You Need

| Requirement | Notes |
|---|---|
| Ubuntu 22.04 LTS (or 24.04) | Other Debian-based distros work |
| Python 3.10+ | Ubuntu 22.04 ships 3.10; 24.04 ships 3.12 |
| FFmpeg 4.4+ with ffprobe | Installed via apt |
| A non-root sudo user | Guide assumes user `iptv` |
| Network multicast support | UDP 239.0.0.1:500X must be routable on your LAN |

---

## Step 1 — Create the server user

```bash
sudo adduser iptv
sudo usermod -aG sudo iptv
su - iptv
```

---

## Step 2 — Install system dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv ffmpeg git curl
```

Verify FFmpeg is installed:
```bash
ffmpeg -version
ffprobe -version
```

---

## Step 3 — Clone the repository

```bash
sudo mkdir -p /opt/iptvmanager
sudo chown iptv:iptv /opt/iptvmanager
cd /opt/iptvmanager
git clone https://github.com/basilkewir/iptvmanager.git .
```

---

## Step 4 — Create Python virtual environment & install dependencies

```bash
cd /opt/iptvmanager
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Step 5 — Create the environment config

```bash
cp .env.example .env
nano .env
```

Set these values (minimum required):

```ini
SECRET_KEY=your-random-secret-here-change-this
APP_PORT=8000
LOG_LEVEL=INFO
DVR_STORAGE_PATH=./data/dvr
DVR_SEGMENT_DURATION=6
DVR_RETENTION_HOURS=2
HEALTH_CHECK_INTERVAL=5
HEALTH_CHECK_TIMEOUT=30
HEALTH_CHECK_FAILURES_BEFORE_DOWN=2
UDP_MULTICAST_BASE=udp://239.0.0.1
UDP_MULTICAST_PORT_START=5000
UDP_TTL=16
FFMPEG_PATH=ffmpeg
FFPROBE_PATH=ffprobe
```

Generate a strong SECRET_KEY:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Step 6 — Create data directories

```bash
mkdir -p /opt/iptvmanager/data/dvr
mkdir -p /opt/iptvmanager/data/logos
```

---

## Step 7 — Test it runs manually

```bash
cd /opt/iptvmanager
source venv/bin/activate
python run.py
```

Open `http://YOUR_SERVER_IP:8000` in a browser.  
**Register your admin account on first visit** — the first user registered becomes the admin.

Press `Ctrl+C` to stop once verified.

---

## Step 8 — Install as a systemd service (runs on boot, auto-restarts)

Create the service file:
```bash
sudo nano /etc/systemd/system/iptvmanager.service
```

Paste exactly:
```ini
[Unit]
Description=IPTV Manager
After=network.target

[Service]
Type=simple
User=iptv
WorkingDirectory=/opt/iptvmanager
ExecStart=/opt/iptvmanager/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable iptvmanager
sudo systemctl start iptvmanager
```

Check it's running:
```bash
sudo systemctl status iptvmanager --no-pager
sudo journalctl -u iptvmanager -f --no-pager
```

---

## Step 9 — Open firewall port (if ufw is active)

```bash
sudo ufw allow 8000/tcp
sudo ufw status
```

---

## Step 10 — Verify

Open `http://YOUR_SERVER_IP:8000` — you should see the login page.

---

## Multicast routing (important for multiple devices)

UDP multicast streams are sent to `239.0.0.1:5001`, `5002`, etc. (one port per stream, based on stream ID).

If your receivers are on a different subnet or VLAN, you need to enable multicast routing on your switch/router, or add a static multicast route on the server:

```bash
# Allow multicast on your LAN interface (e.g. eth0)
sudo ip route add 239.0.0.0/8 dev eth0
```

To make this persistent across reboots, add to `/etc/rc.local` or a systemd network script.

---

## Updating (after initial install)

All future updates are a single command:
```bash
bash /opt/iptvmanager/deploy.sh
```

This will:
1. Check disk space (aborts if >90% full)
2. Back up the database
3. Pull latest code from GitHub
4. Install any new Python packages
5. Auto-migrate database schema (non-destructive — adds new columns only)
6. Restart the service

---

## Directory layout

```
/opt/iptvmanager/
├── app/                  # FastAPI application
│   ├── main.py           # App entry point, /api/health
│   ├── engine.py         # Stream engine (FFmpeg, health checks, DVR)
│   ├── models.py         # SQLAlchemy models
│   ├── schemas.py        # Pydantic schemas
│   ├── config.py         # Settings from .env
│   ├── auth.py           # JWT + bcrypt auth
│   ├── database.py       # Async SQLite session
│   └── routes/
│       ├── auth.py       # Login/register endpoints
│       └── streams.py    # Stream CRUD, start/stop, DVR, WebSocket
├── static/
│   └── index.html        # Single-page web UI
├── data/
│   ├── iptvmanager.db    # SQLite database (never deleted by deploy)
│   ├── dvr/              # DVR segment files per stream
│   │   └── <rtmp_key>/seg_YYYYMMDD_HHMMSS.ts
│   └── logos/            # Uploaded/downloaded logo images
├── .env                  # Your config (never committed to git)
├── requirements.txt
├── run.py
└── deploy.sh             # One-command update script
```

---

## Health check endpoint

No auth required — use this for external monitoring (Uptime Kuma, Zabbix, etc.):

```
GET http://YOUR_SERVER_IP:8000/api/health
```

Response:
```json
{
  "status": "ok",
  "uptime_seconds": 3600,
  "streams": { "total": 5, "live": 4, "dvr": 1, "down": 0 }
}
```

---

## Troubleshooting

**Service won't start**
```bash
sudo journalctl -u iptvmanager -n 50 --no-pager
```

**No video on UDP multicast**
- Check stream status in the Web UI (Live Status tab)
- Check FFmpeg logs in the Web UI (📋 Logs button on the stream card)
- Confirm FFmpeg can reach the source: `ffprobe -v error -i YOUR_SOURCE_URL`
- Confirm your client is joining the correct multicast group: `udp://239.0.0.1:500X`

**Logo not showing**
- Logo X/Y are **percentages** (0–95), not pixels
- `5,5` = near top-left; `80,5` = near top-right; `5,85` = near bottom-left
- The engine probes the video resolution automatically and scales the logo to 10% of video width

**DVR not recording**
- DVR recording only runs while the source is LIVE
- Check disk space: `df -h /opt/iptvmanager/data`
- Check stream has `DVR Enabled` ticked in Edit

**Segments disappear too fast**
- Increase `DVR Retention (hours)` on the stream Edit form (up to 168h / 7 days)
- Watch disk usage — each hour of HD content ≈ 1–3 GB

**Database locked errors**
- This is SQLite under high stream count; normal in logs, does not affect operation
- For >20 simultaneous streams, consider migrating to PostgreSQL
