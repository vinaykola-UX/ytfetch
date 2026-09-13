#!/usr/bin/env bash
# YTFetch one-command deploy (Ubuntu/Debian).
# Installs system deps, creates a venv, registers a systemd service, starts it.
#   sudo ./deploy.sh            → serves on port 8000
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_USER="$(whoami)"
PORT="${PORT:-8000}"
VENV="$APP_DIR/.venv"

echo "==> Installing system packages (python3, venv, ffmpeg)…"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg

echo "==> Creating virtualenv + installing Python deps…"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Registering systemd service (port $PORT)…"
cat > /etc/systemd/system/ytfetch.service <<EOF
[Unit]
Description=YTFetch - YouTube to MP4 downloader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python -m uvicorn app:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ytfetch
systemctl restart ytfetch
sleep 2
systemctl --no-pager --lines=5 status ytfetch || true

echo
echo "✅ YTFetch is running:  http://YOUR_SERVER_IP:$PORT"
echo "   (open port $PORT in your cloud firewall/security group first)"
echo "   Logs:  journalctl -u ytfetch -f"
echo "   Stop:  systemctl stop ytfetch"
