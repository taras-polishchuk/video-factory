#!/usr/bin/env bash
# dns-01-tls-bootstrap.sh: optional preflight that proves Cloudflare
# DNS-01 issuance works for <domain> before the first real deploy.
#
# Not part of the normal deploy: the production Caddy container gets and
# renews its certificates itself (rendered Caddyfile, `dns cloudflare`).
# Use this to check the token scope / zone first.
#
# Usage:
#   CLOUDFLARE_API_TOKEN=... ./scripts/dns-01-tls-bootstrap.sh <domain> [data-dir]
#
# - Builds the Caddy image from infra/caddy/Dockerfile (the stock caddy:2
#   image has no Cloudflare DNS module, A2 F-27) unless VF_CADDY_IMAGE
#   names an existing image.
# - Runs it with a throwaway Caddyfile (DNS-01, no ports published) and
#   a data dir (default: a new temp dir, printed at the end). Point
#   data-dir at the caddy_data volume path to pre-seed it.
# - Waits up to VF_TLS_WAIT seconds (default 120) for <domain>.crt;
#   exit 0 if it appears, 1 otherwise.
# The token is passed by name (-e CLOUDFLARE_API_TOKEN), never in argv.

set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "Usage: $0 <domain> [data-dir]" >&2
  exit 2
fi
if ! printf '%s' "$DOMAIN" | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$'; then
  echo "ERROR: invalid domain '$DOMAIN'" >&2
  exit 2
fi
: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN must be set}"
export CLOUDFLARE_API_TOKEN
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not on PATH" >&2; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${2:-$(mktemp -d "${TMPDIR:-/tmp}/vf-caddy-data.XXXXXX")}"
mkdir -p "$DATA_DIR"
WAIT="${VF_TLS_WAIT:-120}"

IMAGE="${VF_CADDY_IMAGE:-video-factory-caddy:tls-bootstrap}"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "bootstrap: building $IMAGE from infra/caddy/Dockerfile"
  docker build -q -t "$IMAGE" "$ROOT/infra/caddy" >/dev/null
fi

CADDYFILE="$(mktemp "${TMPDIR:-/tmp}/vf-bootstrap-caddyfile.XXXXXX")"
NAME="vf-tls-bootstrap-$$"
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -f "$CADDYFILE"
}
trap cleanup EXIT

cat > "$CADDYFILE" <<EOF
{
	admin off
}
${DOMAIN} {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	respond "bootstrap OK" 200
}
EOF
chmod 0644 "$CADDYFILE"

echo "bootstrap: requesting a certificate for $DOMAIN via DNS-01"
docker run -d --name "$NAME" \
  -e CLOUDFLARE_API_TOKEN \
  -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" \
  -v "$DATA_DIR:/data" \
  "$IMAGE" caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null

found=""
for _ in $(seq 1 "$WAIT"); do
  found="$(find "$DATA_DIR/caddy/certificates" -type f -name "${DOMAIN}.crt" 2>/dev/null | head -n 1 || true)"
  [ -n "$found" ] && break
  docker ps -q --filter "name=^${NAME}$" | grep -q . || break
  sleep 1
done

if [ -z "$found" ]; then
  echo "ERROR: no certificate for $DOMAIN after ${WAIT}s. Caddy log tail:" >&2
  docker logs --tail 30 "$NAME" >&2 2>&1 || true
  exit 1
fi
echo "bootstrap: certificate OK: ${found#"$DATA_DIR"/}"
echo "bootstrap: data dir $DATA_DIR"
