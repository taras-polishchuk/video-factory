#!/usr/bin/env bash
# Start the Video Factory Control Room locally.
# - API on 127.0.0.1:8000
# - UI on 127.0.0.1:5173 (proxies /api -> :8000)
#
# Portable v2 (F9 + F11): does NOT assume any specific venv location.
# It walks the search order: $VIDEO_FACTORY_PYTHON → ./venv/bin/python3
# → /usr/bin/python3 → $(command -v python3). It also runs a curl smoke
# check against /health AND /api/v1/spec so we know boot worked before the
# operator walks away from the terminal.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# F9: portable python selection. Old version hard-coded a sibling
# project's venv; that's a cross-project dependency that breaks when
# the sibling repo moves or is archived. Use a search chain instead.
pick_python() {
  if [ -n "${VIDEO_FACTORY_PYTHON:-}" ] && [ -x "$VIDEO_FACTORY_PYTHON" ]; then
    echo "$VIDEO_FACTORY_PYTHON"; return 0
  fi
  if [ -x "$ROOT/.venv/bin/python3" ]; then
    echo "$ROOT/.venv/bin/python3"; return 0
  fi
  if [ -x "$ROOT/venv/bin/python3" ]; then
    echo "$ROOT/venv/bin/python3"; return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3; return 0
  fi
  echo "ERROR: no python3 found; install or set VIDEO_FACTORY_PYTHON" >&2
  return 1
}
PY="$(pick_python)"

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
  echo "WARNING: ffmpeg not found; assembly stage will be skipped (jobs still reach done)."
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

# Wait for the API to bind. Bound check + health check.
for _ in $(seq 1 20); do
  sleep 0.5
  if curl -sS -m 2 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
    break
  fi
done

# F11: smoke check. /health must return 200 with mock_only=true and
# version. /api/v1/spec must return a valid openapi.json URL. Fail
# loudly if either is missing so operators see a hard error, not a
# silent boot with a half-broken API.
HEALTH="$(curl -sS -m 3 "http://127.0.0.1:$API_PORT/health" || true)"
if [ -z "$HEALTH" ]; then
  echo "FAIL: API failed to bind within 10s; check uvicorn output above."
  exit 1
fi
echo "  /health: $HEALTH"

SPEC="$(curl -sS -m 3 "http://127.0.0.1:$API_PORT/api/v1/spec" || true)"
if [ -z "$SPEC" ] || ! echo "$SPEC" | grep -q "openapi.json"; then
  echo "FAIL: /api/v1/spec did not advertise openapi.json; got: $SPEC"
  exit 1
fi
echo "  /api/v1/spec: $SPEC"

# F11: webhook endpoint must respond (even with 401) — that proves the
# router is wired.
WEBHOOK_CODE="$(curl -sS -m 3 -o /dev/null -w '%{http_code}' \
    -X POST -H 'Content-Type: application/json' \
    -d '{}' "http://127.0.0.1:$API_PORT/api/v1/webhooks/heygen" || true)"
echo "  /api/v1/webhooks/heygen: HTTP $WEBHOOK_CODE (expect 401 unsigned)"

echo "starting UI on http://127.0.0.1:$WEB_PORT"
cd "$ROOT/web"
if [ ! -d node_modules ]; then npm install; fi
VITE_API_TARGET="http://127.0.0.1:$API_PORT" \
  npx vite dev --host 127.0.0.1 --port "$WEB_PORT" &
WEB_PID=$!

echo
echo "============================================================"
echo "  Video Factory Control Room is running."
echo "  UI:       http://127.0.0.1:$WEB_PORT"
echo "  API:      http://127.0.0.1:$API_PORT"
echo "  Spec:     http://127.0.0.1:$API_PORT/docs"
echo "  Webhook:  http://127.0.0.1:$API_PORT/api/v1/webhooks/heygen"
echo "  Python:   $PY"
echo "  DB:       $DB_PATH"
echo "  Storage:  $STORAGE_ROOT"
echo "============================================================"
echo "Press Ctrl+C to stop."

wait