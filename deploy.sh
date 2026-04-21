#!/bin/bash
# ── IPTV Manager Update Script ──
# Run on server: bash /opt/iptvmanager/deploy.sh
# Pulls latest code, updates deps, migrates DB columns, restarts service.
# Your data (DB, DVR segments, logos) is NEVER touched.

set -e
APP_DIR="/opt/iptvmanager"
DB_PATH="$APP_DIR/data/iptvmanager.db"

cd "$APP_DIR"

echo ""
echo "════════════════════════════════════════"
echo "  📺 IPTV Manager — Update"
echo "════════════════════════════════════════"

# ── 0. Pre-flight checks ─────────────────────────────────────────────────
echo ""
echo "🔍 [0/4] Pre-flight checks..."
DISK_PCT=$(df "$APP_DIR" | awk 'NR==2{gsub(/%/,"",$5); print $5}')
if [ "$DISK_PCT" -ge 90 ]; then
    echo "   ❌ Disk is ${DISK_PCT}% full — aborting deploy to protect DVR data."
    echo "      Free up space first: du -sh $APP_DIR/data/dvr/*"
    exit 1
fi
echo "   ✅ Disk OK (${DISK_PCT}% used)"

# Backup DB before any migration
if [ -f "$DB_PATH" ]; then
    BACKUP="${DB_PATH}.bak"
    cp "$DB_PATH" "$BACKUP"
    echo "   ✅ DB backed up → $BACKUP"
fi
IPTV Manager Update Script ──
# Run on server: bash /opt/iptvmanager/deploy.sh
# Pulls latest code, updates deps, migrates DB columns, restarts service.
# Your data (DB, DVR segments, logos) is NEVER touched.

set -e
APP_DIR="/opt/iptvmanager"
DB_PATH="$APP_DIR/data/iptvmanager.db"

cd "$APP_DIR"

echo ""
echo "════════════════════════════════════════"
echo "  �  IPTV Manager — Update"
echo "════════════════════════════════════════"

# ── 1. Pull latest code ──────────────────────────────────────────────────
echo ""
echo "📥 [1/4] Pulling latest code from GitHub..."
git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "   ✅ Already up to date ($(git log -1 --format='%h %s'))"
else
    echo "   ⬆️  $LOCAL → $REMOTE"
    git reset --hard origin/main
    echo "   ✅ Code updated ($(git log -1 --format='%h %s'))"
fi

# ── 2. Install / update Python dependencies ─────────────────────────────
echo ""
echo "📦 [2/4] Installing dependencies..."
source "$APP_DIR/venv/bin/activate"
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "   ✅ Dependencies up to date"

# ── 3. Auto-migrate SQLite database columns ──────────────────────────────
echo ""
echo "🗄️  [3/4] Checking database schema..."
if [ ! -f "$DB_PATH" ]; then
    echo "   ℹ️  No database found — it will be created on first start."
else
    python3 - <<'PYEOF'
import sqlite3, sys

DB = "/opt/iptvmanager/data/iptvmanager.db"
conn = sqlite3.connect(DB)
cur = conn.cursor()

# ── streams table ────────────────────────────────────────────────────────
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='streams'")
if not cur.fetchone():
    print("   ℹ️  streams table not yet created — will be made on first start.")
    conn.close()
    sys.exit(0)

cur.execute("PRAGMA table_info(streams)")
cols = [row[1] for row in cur.fetchall()]

migrations = [
    ("logo_path",            "ALTER TABLE streams ADD COLUMN logo_path VARCHAR"),
    ("logo_x",               "ALTER TABLE streams ADD COLUMN logo_x INTEGER DEFAULT 10"),
    ("logo_y",               "ALTER TABLE streams ADD COLUMN logo_y INTEGER DEFAULT 10"),
    ("dvr_enabled",          "ALTER TABLE streams ADD COLUMN dvr_enabled BOOLEAN DEFAULT 1"),
    ("dvr_hours",            "ALTER TABLE streams ADD COLUMN dvr_hours INTEGER DEFAULT 2"),
    ("consecutive_failures", "ALTER TABLE streams ADD COLUMN consecutive_failures INTEGER DEFAULT 0"),
    ("last_online",          "ALTER TABLE streams ADD COLUMN last_online DATETIME"),
    ("last_checked",         "ALTER TABLE streams ADD COLUMN last_checked DATETIME"),
]

added = []
for col, sql in migrations:
    if col not in cols:
        cur.execute(sql)
        added.append(col)

conn.commit()
conn.close()

if added:
    print(f"   ✅ Added missing columns: {', '.join(added)}")
else:
    print("   ✅ Schema is up to date — no changes needed.")
PYEOF
fi

# ── 4. Restart service ───────────────────────────────────────────────────
echo ""
echo "🔄 [4/4] Restarting service..."
sudo systemctl restart iptvmanager
sleep 3

echo ""
echo "════════════════════════════════════════"
if systemctl is-active --quiet iptvmanager; then
    IP=$(hostname -I | awk '{print $1}')
    echo "  ✅ IPTV Manager is running!"
    echo ""
    echo "  🌐 Web UI:   http://${IP}:8000"
    echo "  📋 Logs:     sudo journalctl -u iptvmanager -f --no-pager"
    echo "  📊 Status:   sudo systemctl status iptvmanager --no-pager"
else
    echo "  ❌ Service failed to start!"
    echo ""
    sudo journalctl -u iptvmanager -n 30 --no-pager
fi
echo "════════════════════════════════════════"
echo ""
