#!/usr/bin/env bash
# IPTV Manager — Ubuntu Server Setup & Run Script
set -e

echo "══════════════════════════════════════════"
echo "  IPTV Manager — Setup"
echo "══════════════════════════════════════════"

# 1. System deps
echo "[1/4] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv ffmpeg

# 2. Python venv
echo "[2/4] Creating Python virtual environment..."
cd "$(dirname "$0")"
python3 -m venv venv
source venv/bin/activate

# 3. Python deps
echo "[3/4] Installing Python packages..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. Config
echo "[4/4] Setting up config..."
if [ ! -f .env ]; then
    cp .env.example .env
    SECRET=$(openssl rand -hex 32)
    sed -i "s/change-me-in-production-use-openssl-rand-hex-32/$SECRET/" .env
    echo "  → Created .env with random secret key"
else
    echo "  → .env already exists, skipping"
fi

echo ""
echo "══════════════════════════════════════════"
echo "  ✅ Setup complete!"
echo ""
echo "  Start the server:"
echo "    source venv/bin/activate"
echo "    python run.py"
echo ""
echo "  Then open: http://YOUR_SERVER_IP:8000"
echo "  Register a user on first visit."
echo "══════════════════════════════════════════"
