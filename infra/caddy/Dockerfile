# Caddy with the Cloudflare DNS module (A2 F-27).
#
# The stock caddy:2 image has NO dns.providers.cloudflare module, so the
# `dns cloudflare` line in the Caddyfile fails to load there. This image
# builds Caddy with xcaddy + github.com/caddy-dns/cloudflare and fails the
# build if the module is missing.
#
# Build (from the repo root):  docker build -t video-factory-caddy:dev infra/caddy
# Normally built by docker compose (docker-compose.production.yml).

# Bases pinned by digest (A2 F-25). Bump both together:
#   docker buildx imagetools inspect caddy:2-builder-alpine
#   docker buildx imagetools inspect caddy:2-alpine
ARG CADDY_BUILDER_IMAGE=caddy:2.11.7-builder-alpine@sha256:aa705b1e8e4bce41a7a30de934c1e00f6821667c1d1206424465065c92cb7674
ARG CADDY_RUNTIME_IMAGE=caddy:2.11.7-alpine@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f

FROM ${CADDY_BUILDER_IMAGE} AS build
# Pinned module version (git tag of caddy-dns/cloudflare).
ARG CADDY_DNS_CLOUDFLARE_VERSION=v0.2.4
RUN xcaddy build --output /usr/bin/caddy \
        --with github.com/caddy-dns/cloudflare@${CADDY_DNS_CLOUDFLARE_VERSION}

FROM ${CADDY_RUNTIME_IMAGE}
COPY --from=build /usr/bin/caddy /usr/bin/caddy
# Fail the build, not the first deploy, if the DNS module is absent.
RUN caddy version \
    && caddy list-modules | grep -qx 'dns.providers.cloudflare'

# The admin API (127.0.0.1:2019, Caddyfile global options) answers only
# once a config is loaded.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:2019/config/ || exit 1
