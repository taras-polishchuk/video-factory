#!/usr/bin/env bash
# dns-01-tls-bootstrap.sh — issue a wildcard TLS cert via DNS-01
#
# This is a thin wrapper around `caddy` running in DNS-challenge mode.
# We need DNS-01 when port 80 is unreachable (firewall, NAT, etc.)
# which is the common case for fresh Hetzner boxes behind Caddy.
#
# Usage:
#   ./scripts/dns-01-tls-bootstrap.sh <domain>
#
# Required env:
#   CLOUDFLARE_API_TOKEN  — DNS edit on the zone
#
# Optional env:
#   CLOUDFLARE_ZONE_ID    — if not set, the script discovers it via the API
#   Caddy image           — default `caddy:2`
#
# After running, the cert + chain are written to ./caddy-data/caddy/
# inside the repo. The deploy.sh script mounts that path into the
# running Caddy container.

set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  cat <<USAGE
Usage: $0 <domain>
Issues a Let's Encrypt wildcard cert for <domain> via Cloudflare DNS-01.
USAGE
  exit 2
fi

: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN must be set}"

CADDY_DATA="$PWD/caddy-data"
mkdir -p "$CADDY_DATA/caddy" "$CADDY_DATA/config" "$CADDY_DATA/proxy"

# Caddy uses the cloudflare DNS plugin via a runtime container. We
# pin the official image (`caddy:2-builder` includes the plugin).
# If docker is missing, we fall back to a local caddy binary if one
# is on PATH.
if command -v docker >/dev/null 2>&1; then
  RUNNER="docker"
  RUNTIME_IMAGE="caddy:2"
else
  RUNNER="caddy"
  RUNTIME_IMAGE=""
  if ! command -v caddy >/dev/null 2>&1; then
    echo "ERROR: neither docker nor caddy is available" >&2
    exit 1
  fi
fi

BOOTSTRAP_CADDYFILE="$(mktemp)"
trap 'rm -f "$BOOTSTRAP_CADDYFILE"' EXIT

cat > "$BOOTSTRAP_CADDYFILE" <<EOF
{
    email admin@${DOMAIN#*.}
    on_demand_tls {
        ask https://example.com/no-ask-endpoint  # disabled by default
    }
}

*.${DOMAIN}, ${DOMAIN} {
    tls {
        dns cloudflare {env.CLOUDFLARE_API_TOKEN}
    }
    respond "bootstrap OK" 200
}
EOF

echo "bootstrap: requesting cert for *.$DOMAIN via DNS-01"

if [ "$RUNNER" = "docker" ]; then
  docker run --rm \
    -e CLOUDFLARE_API_TOKEN \
    -v "$BOOTSTRAP_CADDYFILE:/etc/caddy/Caddyfile:ro" \
    -v "$CADDY_DATA:/data" \
    "$RUNTIME_IMAGE" caddy run --config /etc/caddy/Caddyfile --adapter ""
else
  CLOUDFLARE_API_TOKEN="$CLOUDFLARE_API_TOKEN" \
    caddy run --config "$BOOTSTRAP_CADDYFILE" --adapter ""
fi &
CADDY_PID=$!

# Give it a few seconds to acquire the cert.
for _ in $(seq 1 30); do
  sleep 1
  # compgen -G expands the glob safely ([ -f glob ] errors on 2+ matches).
  if compgen -G "$CADDY_DATA/caddy/certificates/acme-v02.api.letsencrypt.org-directory/*/*.crt" >/dev/null 2>&1; then
    break
  fi
done

kill "$CADDY_PID" 2>/dev/null || true
wait "$CADDY_PID" 2>/dev/null || true

# Certs live one level down: certificates/<issuer>/<name>/<name>.crt.
if [ -z "$(find "$CADDY_DATA/caddy/certificates" -name '*.crt' -print -quit 2>/dev/null)" ]; then
  echo "ERROR: DNS-01 bootstrap did not produce a cert under $CADDY_DATA/caddy/certificates" >&2
  exit 1
fi

echo "bootstrap: cert acquired under $CADDY_DATA/caddy"
echo "bootstrap: done — proceed with ./scripts/deploy.sh $DOMAIN"