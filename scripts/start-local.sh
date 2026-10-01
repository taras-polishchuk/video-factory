#!/usr/bin/env bash
# Start the Video Factory Control Room locally.
# - API on 127.0.0.1:8000
# - UI on 127.0.0.1:5173 (proxies /api -> :8000)
#
# This script expects Python 3.11+ with pip. It installs the Python
# deps into --user if not already present, then launches both
# processes. Ctrl+C stops both.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
VENV_PY="/home/taras/projects/founderos-core/.venv/bin/python3"
if [ -x "$VENV_PY" ]; then PY="$VENV_PY"; fi

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-5173}"
DB_PATH="${VIDEO_FACTORY_DB:-$ROOT/artifacts/video_factory.db}"
STORAGE_ROOT="${VIDEO_FACTORY_STORAGE:-$ROOT/artifacts/storage}"
MAX_COST_VIDEO="${MAX_COST_PER_VIDEO_USD:-5}"
MAX_COST_BATCH="${MAX_COST_PER_BATCH_USD:-20}"

mkdir -p "$(dirname "$DB_PATH")" "$STORAGE_ROOT"

if ! "$PY" -c 'import fastapi, uvicorn, jsonschema, multipart' >/dev/null 2>&1; then
  echo "installing python deps..."
  "$PY" -m pip install --user 'jsonschema>=4.0' 'fastapi>=0.115' \
                              'uvicorn>=0.30' 'python-multipart>=0.0.9' || true
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "WARNING: ffmpeg not found; mock videos will be skipped, job will end at assembly stage with an error."
fi

cleanup() {
  echo
  echo "stopping..."
  [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null || true
  [ -n "${WEB_PID:-}" ] && kill "$WEB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "starting API on http://127.0.0.1:$API_PORT"
PYTHONPATH="$ROOT" \
  MAX_COST_PER_VIDEO_USD="$MAX_COST_VIDEO" \
  MAX_COST_PER_BATCH_USD="$MAX_COST_BATCH" \
  VIDEO_FACTORY_DB="$DB_PATH" \
  VIDEO_FACTORY_STORAGE="$STORAGE_ROOT" \
  "$PY" -m uvicorn api.main:app --host 127.0.0.1 --port "$API_PORT" &
API_PID=$!

# Give the API a moment to bind.
sleep 2

if ! curl -sS -m 3 "http://127.0.0.1:$API_PORT/health" >/dev/null; then
  echo "API failed to start; check uvicorn output above."
  exit 1
fi

echo "starting UI on http://127.0.0.1:$WEB_PORT"
cd "$ROOT/web"
if [ ! -d node_modules ]; then npm install; fi
VITE_API_TARGET="http://127.0.0.1:$API_PORT" \
  npx vite dev --host 127.0.0.1 --port "$WEB_PORT" &
WEB_PID=$!

echo
echo "============================================================"
echo "  Video Factory Control Room is running."
echo "  UI:  http://127.0.0.1:$WEB_PORT"
echo "  API: http://127.0.0.1:$API_PORT"
echo "  DB:  $DB_PATH"
echo "============================================================"
echo "Press Ctrl+C to stop."

wait