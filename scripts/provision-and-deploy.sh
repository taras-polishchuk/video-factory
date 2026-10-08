#!/usr/bin/env bash
# provision-and-deploy.sh — tofu apply → wait → rsync → docker compose up
#
# Assumes the operator has run `./scripts/deploy.sh <domain>` first
# (which created tofu state + wrote infra/hosts/<domain>.env).
#
# Usage:
#   ./scripts/provision-and-deploy.sh <domain> [--apply]
#
# Without --apply: prints the plan and exits 0 (dry run).
# With --apply:    actually creates / updates infrastructure.
#
# Idempotency:
#   - tofu apply is inherently idempotent at the resource level
#   - rsync uses --delete only on the controlled paths we own
#   - docker compose up -d is idempotent
#   - re-running after a successful first run is a no-op for the
#     tofu step (no changes) and a no-op for compose (services up).

set -euo pipefail

DOMAIN="${1:-}"
APPLY=0

shift || true
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    -h|--help)
      cat <<USAGE
Usage: $0 <domain> [--apply]

Provisions Hetzner + DNS via OpenTofu, rsyncs the repo to the new
host, then starts docker compose services.

Without --apply: shows the plan and exits 0.
With --apply:    runs apply + rsync + compose up.
USAGE
      exit 0
      ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ -z "$DOMAIN" ]; then
  echo "ERROR: <domain> argument required" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$ROOT/infra/opentofu"
HOSTS_DIR="$ROOT/infra/hosts"
HOST_ENV="$HOSTS_DIR/$DOMAIN.env"

if [ ! -d "$TOFU_DIR" ]; then
  echo "ERROR: $TOFU_DIR does not exist" >&2
  exit 1
fi

if [ ! -f "$HOST_ENV" ]; then
  echo "ERROR: $HOST_ENV does not exist; run ./scripts/deploy.sh $DOMAIN first" >&2
  exit 1
fi

# Load the host env (contains the server IP from the prior tofu run).
set -a
# shellcheck disable=SC1090
. "$HOST_ENV"
set +a

if [ -z "${VIDEO_FACTORY_SERVER_IP:-}" ]; then
  echo "ERROR: $HOST_ENV did not export VIDEO_FACTORY_SERVER_IP" >&2
  exit 1
fi

# ─── 1) tofu init + plan (always; cheap, no side effects) ──────
cd "$TOFU_DIR"
tofu init -backend=false >/dev/null
tofu plan -input=false -var "domain=$DOMAIN"

if [ "$APPLY" -eq 0 ]; then
  echo "plan complete; re-run with --apply to actually create resources"
  exit 0
fi

# ─── 2) tofu apply (real) ──────────────────────────────────────
tofu apply -input=false -auto-approve -var "domain=$DOMAIN"

# Refresh the host env with the actual server IP. `tofu output -raw`
# requires the working dir to be the tofu dir.
NEW_IP="$(tofu output -raw server_ipv4)"
sed -i.bak "s|^VIDEO_FACTORY_SERVER_IP=.*|VIDEO_FACTORY_SERVER_IP=$NEW_IP|" "$HOST_ENV"
rm -f "$HOST_ENV.bak"
VIDEO_FACTORY_SERVER_IP="$NEW_IP"
export VIDEO_FACTORY_SERVER_IP

# ─── 3) Wait for SSH on the new host ───────────────────────────
echo "wait: probing ssh root@$NEW_IP:22"
for i in $(seq 1 60); do
  sleep 2
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=3 \
       -o BatchMode=yes "root@$NEW_IP" true 2>/dev/null; then
    echo "wait: ssh ready after ${i} attempts"
    break
  fi
done

if ! ssh -o StrictHostKeyChecking=no -o ConnectTimeout=3 \
       -o BatchMode=yes "root@$NEW_IP" true 2>/dev/null; then
  echo "ERROR: ssh root@$NEW_IP did not respond within 120s" >&2
  exit 1
fi

# ─── 4) rsync the repo ────────────────────────────────────────
RSYNC_EXCLUDES=(
  --exclude '.venv'
  --exclude 'node_modules'
  --exclude '.svelte-kit'
  --exclude 'artifacts'
  --exclude '__pycache__'
  --exclude '.pytest_cache'
  --exclude '.git'
  --exclude 'infra/opentofu/.terraform'
)
rsync -az --delete "${RSYNC_EXCLUDES[@]}" \
  "$ROOT/" "root@$NEW_IP:/opt/video-factory/"

# ─── 5) render compose env, push systemd credentials, compose up ─
ssh "root@$NEW_IP" bash -s <<REMOTE
set -euo pipefail
cd /opt/video-factory

# Render the production compose env from the host env. Any variable
# that ends in _SECRET / _TOKEN / _KEY / _PASSWORD is sourced from
# systemd LoadCredential= and never appears in the .env file (see
# systemd/secret-distribution.md).
{
  echo "VIDEO_FACTORY_DOMAIN=$DOMAIN"
  echo "VIDEO_FACTORY_SERVER_IP=$VIDEO_FACTORY_SERVER_IP"
  echo "CLOUDFLARE_API_TOKEN=\${CLOUDFLARE_API_TOKEN}"
  echo "HETZNER_TOKEN=\${HETZNER_TOKEN}"
  echo "STRIPE_SECRET_KEY=\${STRIPE_SECRET_KEY}"
  echo "STRIPE_WEBHOOK_SECRET=\${STRIPE_WEBHOOK_SECRET}"
  echo "VIDEO_FACTORY_SECRET_KEY=\${VIDEO_FACTORY_SECRET_KEY}"
  echo "VIDEO_FACTORY_DB_KEY=\${VIDEO_FACTORY_DB_KEY}"
  echo "VIDEO_FACTORY_SESSION_SECRET=\${VIDEO_FACTORY_SESSION_SECRET}"
} > /opt/video-factory/.env.production
chmod 0600 /opt/video-factory/.env.production

docker compose --env-file .env.production up -d --remove-orphans

# Smoke: the api container must respond on /health within 30s.
for i in \$(seq 1 30); do
  sleep 1
  if docker compose exec -T api curl -fsS http://127.0.0.1:8000/health >/dev/null; then
    echo "smoke: /health 200"
    exit 0
  fi
done
echo "smoke: api did not respond to /health in 30s" >&2
exit 1
REMOTE

echo "deploy: $DOMAIN is live on https://$DOMAIN"