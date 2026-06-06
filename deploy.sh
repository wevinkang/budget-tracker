#!/usr/bin/env bash
# Deploy local source to the Raspberry Pi and rebuild the container.
# Skips: venv, __pycache__, .git, budget.db, .env (Pi keeps its own).
set -euo pipefail

PI_HOST="${PI_HOST:-wev@192.168.1.10}"
APP_DIR="budget"

cd "$(dirname "$0")"

echo "==> Rsyncing source to ${PI_HOST}:~/${APP_DIR}/"
rsync -az --delete \
  --exclude='venv/' \
  --exclude='__pycache__/' \
  --exclude='.git/' \
  --exclude='budget.db' \
  --exclude='budget.db.bak' \
  --exclude='budget.db.tmp' \
  --exclude='*.zip' \
  --exclude='.env' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  ./ "${PI_HOST}:${APP_DIR}/"

echo "==> Rebuilding & restarting container"
ssh "${PI_HOST}" "cd ~/${APP_DIR} && sudo docker compose up -d --build"

echo "==> Status"
ssh "${PI_HOST}" "cd ~/${APP_DIR} && sudo docker compose ps"

echo
echo "Done."
echo "  LAN:     http://192.168.1.10:5000"
echo "  Tailnet: http://100.106.33.38:5000"
