#!/usr/bin/env bash
# provision-and-deploy.sh: tofu plan/apply -> bootstrap server -> ship
# code, config and credentials -> build -> start the systemd service.
#
# Called by ./scripts/deploy.sh <domain> (which validates env, applies
# the ADMIN_CIDR guard, writes infra/hosts/<domain>.env and renders
# infra/hosts/<domain>/Caddyfile).
#
# Usage:
#   ./scripts/provision-and-deploy.sh <domain> [--apply]
#
# Without --apply: tofu plan only, exits 0. No side effects.
# With --apply:    apply the reviewed plan file, then deploy.
#
# Guards:
#   - `tofu plan -detailed-exitcode -out=<plan>`; exit 1 = error, stop.
#   - A plan that deletes or replaces any resource is refused unless
#     ALLOW_DESTROY=1 (A2 F-34).
#   - Only the exact plan that was shown is applied (`tofu apply <plan>`).
#
# Secrets: tofu reads tokens from TF_VAR_* env vars (never argv). The
# server gets each credential over ssh STDIN into
# /etc/video-factory/credentials/<name> (0600); values never appear in
# argv, in logs or in any file under the repo.
#
# Idempotency: re-running with no infra change = "no changes" plan, then
# re-sync + rebuild + restart.

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

Provisions Hetzner + DNS via OpenTofu, bootstraps the server (docker,
compose, restic, systemd units), ships code + config + credentials and
starts video-factory.service.

Without --apply: shows the plan and exits 0.
With --apply:    applies the plan and deploys.
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
if ! printf '%s' "$DOMAIN" | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$'; then
  echo "ERROR: invalid domain '$DOMAIN'" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOFU_DIR="$ROOT/infra/opentofu"
HOSTS_DIR="$ROOT/infra/hosts"
HOST_ENV="$HOSTS_DIR/$DOMAIN.env"
HOST_CADDYFILE="$HOSTS_DIR/$DOMAIN/Caddyfile"
REMOTE_DIR=/opt/video-factory

if [ ! -d "$TOFU_DIR" ]; then
  echo "ERROR: $TOFU_DIR does not exist" >&2
  exit 1
fi
if [ ! -f "$HOST_ENV" ] || [ ! -f "$HOST_CADDYFILE" ]; then
  echo "ERROR: $HOST_ENV / $HOST_CADDYFILE missing; run ./scripts/deploy.sh $DOMAIN first" >&2
  exit 1
fi

# Host env: DOMAIN, HCLOUD_*, ADMIN_CIDR, VIDEO_FACTORY_SERVER_IP (may be
# empty before the first apply).
set -a
# shellcheck disable=SC1090
. "$HOST_ENV"
set +a

for v in HETZNER_TOKEN CLOUDFLARE_API_TOKEN CLOUDFLARE_ZONE_ID; do
  if [ -z "${!v:-}" ]; then
    echo "ERROR: $v must be set" >&2
    exit 1
  fi
done

# ─── 1) tofu init + plan (always; no side effects) ─────────────
export TF_IN_AUTOMATION=1
export TF_VAR_hcloud_token="$HETZNER_TOKEN"
export TF_VAR_cloudflare_api_token="$CLOUDFLARE_API_TOKEN"
export TF_VAR_cloudflare_zone_id="$CLOUDFLARE_ZONE_ID"
export TF_VAR_domain="$DOMAIN"
export TF_VAR_admin_cidr="${ADMIN_CIDR:-0.0.0.0/0}"
export TF_VAR_server_type="${HCLOUD_SERVER_TYPE:-cx22}"
export TF_VAR_location="${HCLOUD_LOCATION:-fsn1}"

PLAN_FILE="$HOSTS_DIR/$DOMAIN/tfplan"
cd "$TOFU_DIR"
tofu init -input=false >/dev/null

set +e
tofu plan -input=false -detailed-exitcode -out="$PLAN_FILE"
plan_rc=$?
set -e
case "$plan_rc" in
  0) echo "plan: no infrastructure changes" ;;
  2) echo "plan: infrastructure changes pending" ;;
  *) echo "ERROR: tofu plan failed (exit $plan_rc)" >&2; exit 1 ;;
esac

if [ "$plan_rc" -eq 2 ]; then
  destructive="$(tofu show -json "$PLAN_FILE" | python3 -c '
import json, sys
plan = json.load(sys.stdin)
for rc in plan.get("resource_changes", []):
    actions = rc.get("change", {}).get("actions", [])
    if "delete" in actions:
        print(str(rc.get("address", "?")) + ": " + "/".join(actions))
')"
  if [ -n "$destructive" ]; then
    echo "plan: DESTRUCTIVE changes (delete/replace):" >&2
    printf '  %s\n' "$destructive" >&2
    if [ "$APPLY" -eq 1 ] && [ "${ALLOW_DESTROY:-0}" != "1" ]; then
      echo "ERROR: refusing to apply a destructive plan; review it, then re-run with ALLOW_DESTROY=1" >&2
      exit 3
    fi
  fi
fi

if [ "$APPLY" -eq 0 ]; then
  rm -f "$PLAN_FILE"
  echo "plan complete; re-run with APPLY=1 ./scripts/deploy.sh $DOMAIN to apply"
  exit 0
fi

# ─── 2) tofu apply (exactly the plan shown above) ──────────────
if [ "$plan_rc" -eq 2 ]; then
  tofu apply -input=false "$PLAN_FILE"
fi
rm -f "$PLAN_FILE"

NEW_IP="$(tofu output -raw server_ipv4)"
if ! python3 -c 'import ipaddress, sys; ipaddress.IPv4Address(sys.argv[1])' "$NEW_IP" 2>/dev/null; then
  echo "ERROR: tofu output server_ipv4 is not an IPv4 address" >&2
  exit 1
fi
tmp_env="$(mktemp "$HOSTS_DIR/.env.XXXXXX")"
sed "s|^VIDEO_FACTORY_SERVER_IP=.*|VIDEO_FACTORY_SERVER_IP=$NEW_IP|" "$HOST_ENV" > "$tmp_env"
chmod 0600 "$tmp_env"
mv -f "$tmp_env" "$HOST_ENV"
cd "$ROOT"

# accept-new: trust the key on first contact, refuse a changed key later.
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes)
# shellcheck disable=SC2029  # remote commands are built client-side on purpose
remote() { ssh "${SSH_OPTS[@]}" "root@$NEW_IP" "$@"; }

# ─── 3) Wait for SSH on the host ───────────────────────────────
echo "wait: probing ssh root@$NEW_IP:22"
ssh_ok=0
for i in $(seq 1 60); do
  if remote true 2>/dev/null; then
    echo "wait: ssh ready after ${i} attempts"
    ssh_ok=1
    break
  fi
  sleep 2
done
if [ "$ssh_ok" -ne 1 ]; then
  echo "ERROR: ssh root@$NEW_IP did not respond within 120s" >&2
  exit 1
fi

# ─── 4) Ship the code (no secrets, no state, no build output) ──
RSYNC_EXCLUDES=(
  --exclude '.git'
  --exclude '.venv'
  --exclude 'venv'
  --exclude 'node_modules'
  --exclude '.svelte-kit'
  --exclude 'web/build'
  --exclude 'artifacts'
  --exclude '__pycache__'
  --exclude '.pytest_cache'
  --exclude '.env'
  --exclude '.env.*'
  --exclude 'infra/hosts'
  --exclude 'infra/opentofu/.terraform'
  --exclude 'infra/opentofu/*.tfstate*'
  --exclude '*.db'
)
rsync -az --delete -e "ssh ${SSH_OPTS[*]}" "${RSYNC_EXCLUDES[@]}" \
  "$ROOT/" "root@$NEW_IP:$REMOTE_DIR/"

# ─── 5) Bootstrap: docker, compose, restic, systemd units ──────
remote "bash $REMOTE_DIR/scripts/remote-bootstrap.sh"

# ─── 6) Config + Caddyfile + credentials over stdin ────────────
IMAGE_TAG="$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)"
{
  echo "# Written by provision-and-deploy.sh on $(date -u +%FT%TZ). Non-secret."
  echo "VIDEO_FACTORY_DOMAIN=$DOMAIN"
  echo "VIDEO_FACTORY_IMAGE_TAG=$IMAGE_TAG"
  echo "VIDEO_FACTORY_CADDYFILE=/etc/video-factory/Caddyfile"
  if [ -n "${STRIPE_PRO_PRICE_ID:-}" ]; then echo "STRIPE_PRO_PRICE_ID=$STRIPE_PRO_PRICE_ID"; fi
  if [ -n "${STRIPE_ENTERPRISE_PRICE_ID:-}" ]; then echo "STRIPE_ENTERPRISE_PRICE_ID=$STRIPE_ENTERPRISE_PRICE_ID"; fi
} | remote "umask 077 && cat > /etc/video-factory/config.env.tmp && mv -f /etc/video-factory/config.env.tmp /etc/video-factory/config.env"

remote "umask 022 && cat > /etc/video-factory/Caddyfile.tmp && mv -f /etc/video-factory/Caddyfile.tmp /etc/video-factory/Caddyfile" \
  < "$HOST_CADDYFILE"

# name -> env var. HETZNER_TOKEN is deliberately NOT shipped: the
# server never needs it (least privilege).
for pair in \
    cloudflare_api_token:CLOUDFLARE_API_TOKEN \
    stripe_secret_key:STRIPE_SECRET_KEY \
    stripe_webhook_secret:STRIPE_WEBHOOK_SECRET \
    video_factory_secret_key:VIDEO_FACTORY_SECRET_KEY \
    video_factory_db_key:VIDEO_FACTORY_DB_KEY \
    video_factory_session_secret:VIDEO_FACTORY_SESSION_SECRET
do
  name="${pair%%:*}"
  var="${pair#*:}"
  if [ -z "${!var:-}" ]; then
    echo "ERROR: $var must be set" >&2
    exit 1
  fi
  # printf is a shell builtin: the value is not in any argv.
  printf '%s' "${!var}" | remote "umask 077 && cat > /etc/video-factory/credentials/$name.tmp && mv -f /etc/video-factory/credentials/$name.tmp /etc/video-factory/credentials/$name"
done
echo "deploy: config + credentials installed (values not shown)"

# ─── 7) Build + (re)start the service, then smoke inside the host ─
remote bash -s <<'REMOTE'
set -euo pipefail
cd /opt/video-factory
COMPOSE=(docker compose -p video-factory -f docker-compose.production.yml --env-file .env.production)
/usr/local/bin/video-factory-load-env.sh /opt/video-factory/.env.production
"${COMPOSE[@]}" build
systemctl daemon-reload
systemctl enable video-factory.service >/dev/null
systemctl restart video-factory.service
for _ in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T api curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "smoke: api /healthz 200 (inside the host)"
    exit 0
  fi
  sleep 2
done
echo "smoke: api did not answer /healthz within 120s" >&2
"${COMPOSE[@]}" ps >&2 || true
exit 1
REMOTE

echo "deploy: $DOMAIN deployed to $NEW_IP (image tag $IMAGE_TAG)"
