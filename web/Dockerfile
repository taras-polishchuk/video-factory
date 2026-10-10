# Video Factory web UI (SvelteKit + adapter-node): production image.
#
# Build (from the repo root):  docker build -t video-factory-web:dev web
# Normally built by docker compose (docker-compose.production.yml,
# `build: ./web`) and tagged video-factory-web:${VIDEO_FACTORY_IMAGE_TAG}.
#
# Runtime env (compose): HOST, PORT, ORIGIN, BODY_SIZE_LIMIT,
# VIDEO_FACTORY_API_URL (server-side fetches go straight to the API
# container), VIDEO_FACTORY_SESSION_SECRET (logout CSRF helper).

# Base pinned by digest (same rule as the API image, A2 F-25).
# Bump: docker buildx imagetools inspect node:22-alpine
ARG NODE_IMAGE=node:22.23.3-alpine3.24@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402

# ── build stage ───────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS build
WORKDIR /src
# The `playwright` devDependency is for local e2e only: never download
# browsers into the build.
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    npm_config_audit=false \
    npm_config_fund=false \
    npm_config_update_notifier=false
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build \
    && test -f build/index.js

# ── runtime stage ─────────────────────────────────────────────────
# adapter-node bundles every devDependency into build/, so the runtime
# image needs only package.json ("type": "module") and build/. No
# node_modules, no npm cache, no sources.
FROM ${NODE_IMAGE}
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000
WORKDIR /app
COPY --from=build --chown=root:root /src/package.json ./package.json
COPY --from=build --chown=root:root /src/build ./build
# Built-in unprivileged `node` user (uid 1000). The app writes nothing,
# so the root filesystem can be read-only.
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "build/index.js"]
