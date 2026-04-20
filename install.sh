#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════
# IPTV Manager — Fresh Install Script for Ubuntu
# Run as root or with sudo:  sudo bash install.sh
# ══════════════════════════════════════════════════════════════════════════
set -e

APP_DIR="/opt/iptvmanager"
APP_USER="iptv"
REPO="https://github.com/basilkewir/iptvmanager.git"
BRANCH="main"

echo "══════════════════════════════════════════════════"
echo "  📡 IPTV Manager — Fresh Install"
echo "══════════════════════════════════════════════════"

# ── 1. System packages ──────────────────────────────────────────────────
echo ""
echo "📦 [1/7] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip ffmpeg > /dev/null 2>&1
echo "   ✅ git, python3, ffmpeg installed"

# ── 2. Create app user ──────────────────────────────────────────────────
echo ""
echo "👤 [2/7] Setting up user '$APP_USER'..."
if id "$APP_USER" &>/dev/null; then
    echo "   User '$APP_USER' already exists"
else
    useradd -r -m -s /bin/bash "$APP_USER"
    echo "   ✅ User '$APP_USER' created"
fi

# ── 3. Clone repo ───────────────────────────────────────────────────────
echo ""
echo "📥 [3/7] Cloning repository..."
if [ -d "$APP_DIR/.git" ]; then
    echo "   Repo exists — pulling latest..."
    cd "$APP_DIR"
    git fetch origin "$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    rm -rf "$APP_DIR"
    git clone -b "$BRANCH" "$REPO" "$APP_DIR"
fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
echo "   ✅ Code at $APP_DIR"

# ── 4. Python venv + dependencies ───────────────────────────────────────
echo ""
echo "🐍 [4/7] Setting up Python environment..."
cd "$APP_DIR"
sudo -u "$APP_USER" python3 -m venv venv
sudo -u "$APP_USER" bash -c "source venv/bin/activate && pip install -q --upgrade pip && pip install -q -r requirements.txt"
echo "   ✅ Virtual environment ready"

# ── 5. Create data directories ──────────────────────────────────────────
echo ""
echo "📁 [5/7] Creating data directories..."
sudo -u "$APP_USER" mkdir -p "$APP_DIR/data/dvr"
sudo -u "$APP_USER" mkdir -p "$APP_DIR/data/logos"
echo "   ✅ data/dvr and data/logos created"

# ── 6. Create .env file ─────────────────────────────────────────────────
echo ""
echo "⚙️  [6/7] Configuring .env..."
ENV_FILE="$APP_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    echo "   .env already exists — cleaning old Flussonic entries..."
    sed -i '/FLUSSONIC/d' "$ENV_FILE"
else
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    cat > "$ENV_FILE" << EOF
SECRET_KEY=$SECRET
APP_HOST=0.0.0.0
APP_PORT=8000
DATABASE_URL=sqlite+aiosqlite:///./data/iptvmanager.db
DVR_STORAGE_PATH=./data/dvr
HEALTH_CHECK_INTERVAL=5
HEALTH_CHECK_TIMEOUT=30
HEALTH_CHECK_FAILURES_BEFORE_DOWN=3
DVR_SEGMENT_DURATION=6
DVR_RETENTION_HOURS=2
UDP_MULTICAST_BASE=udp://239.0.0.1
UDP_MULTICAST_PORT_START=5000
UDP_TTL=16
UDP_BUFFER_SIZE=1316
LOG_LEVEL=INFO
EOF
    chown "$APP_USER":"$APP_USER" "$ENV_FILE"
    echo "   ✅ .env created with secure secret key"
fi

# ── 7. Create systemd service ───────────────────────────────────────────
echo ""
echo "🔧 [7/7] Setting up systemd service..."
cat > /etc/systemd/system/iptvmanager.service << EOF
[Unit]
Description=IPTV Manager
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python3 run.py
Restart=always
RestartSec=5
Environment=PATH=$APP_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable iptvmanager
systemctl restart iptvmanager

sleep 2

echo ""
echo "══════════════════════════════════════════════════"
if systemctl is-active --quiet iptvmanager; then
    IP=$(hostname -I | awk '{print $1}')
    echo "  ✅ IPTV Manager is running!"
    echo ""
    echo "  🌐 Web UI:  http://${IP}:8000"
    echo "  📁 App dir: $APP_DIR"
    echo "  📋 Logs:    sudo journalctl -u iptvmanager -f"
    echo "  🔄 Update:  bash $APP_DIR/deploy.sh"
    echo ""
    echo "  First visit: Register a new account in the web UI"
else
    echo "  ❌ Service failed to start!"
    echo ""
    echo "  Check logs: sudo journalctl -u iptvmanager -n 50 --no-pager"
fi
echo "══════════════════════════════════════════════════"
