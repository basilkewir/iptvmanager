#!/bin/bash
# ── IPTV Manager Deploy Script ──
# Run on server: bash /opt/iptvmanager/deploy.sh
# Pulls latest code (no auth needed for public repo) and restarts service.

set -e
cd /opt/iptvmanager

echo "📥 Fetching latest from GitHub..."
git fetch origin main

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "✅ Already up to date ($LOCAL)"
    read -p "Restart service anyway? [y/N] " yn
    case $yn in [Yy]*) ;; *) echo "Done."; exit 0;; esac
else
    echo "🔄 Updating: $LOCAL → $REMOTE"
    git reset --hard origin/main
fi

# Activate venv and install any new dependencies
echo "📦 Installing dependencies..."
source venv/bin/activate
pip install -q -r requirements.txt

# Auto-migrate DB: delete old DB if schema changed (SQLite limitation)
# Check if alembic is set up; if not, just recreate DB on model changes
if [ -f data/iptvmanager.db ]; then
    echo "🗄️  Existing database found — keeping it."
    echo "   (If you get schema errors, run: rm -f data/iptvmanager.db)"
fi

echo "🔄 Restarting service..."
sudo systemctl restart iptvmanager

sleep 2
if systemctl is-active --quiet iptvmanager; then
    echo "✅ iptvmanager is running!"
    echo "🌐 http://$(hostname -I | awk '{print $1}'):8000"
else
    echo "❌ Service failed to start. Check logs:"
    echo "   sudo journalctl -u iptvmanager -n 30 --no-pager"
fi
