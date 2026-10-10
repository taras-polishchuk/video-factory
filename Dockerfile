# Video Factory API: production image.
#
# Build (from the repo root):
#   docker build -t video-factory-api:dev .
# Normally built by docker compose (docker-compose.production.yml,
# `build: .`) and tagged video-factory-api:${VIDEO_FACTORY_IMAGE_TAG}.
#
# Runtime contract:
#   - uvicorn listens on 0.0.0.0:8000 INSIDE the container so Caddy and
#     the web container can reach it on the compose network. Nothing
#     publishes 8000 on the host; Caddy is the only public listener.
#     (staging VF-STG-02: 127.0.0.1 made the API unreachable.)
#   - Non-root user `app` (uid/gid 10001). Writable paths: /data
#     (volume: DB, storage, TMPDIR) and /tmp (tmpfs in compose). The
#     root filesystem can be mounted read-only.
#   - --workers 1 is intentional: the JobRunner keeps per-process state
#     (in-memory idempotency map, job threads). Do not scale workers.

# Base pinned by digest (A2 F-25). The tag is for humans; the digest wins.
# Bump: docker buildx imagetools inspect python:3.12-slim-trixie
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# ffmpeg: assembly stage (pulls fontconfig + DejaVu, used by drawtext).
# curl: container healthcheck.
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so this layer caches independently of source.
# requirements.lock = pinned + hashed runtime closure of pyproject.toml
# (scripts/gen_requirements_lock.py). --no-deps + `pip check` proves the
# lock is complete; --only-binary keeps compilers out of the image.
COPY pyproject.toml requirements.lock ./
RUN pip install --no-deps --require-hashes --only-binary=:all: -r requirements.lock \
    && pip check

# Application source (.dockerignore keeps venvs, node_modules, tests,
# artifacts, env files and git metadata out).
COPY . /app

# The app runs from /app (schemas/ and core/migrations resolve relative
# to the source tree), so the project itself is not pip-installed.
# Bytecode is precompiled because the root filesystem is read-only at
# runtime.
RUN python -m compileall -q /app \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home \
         --shell /usr/sbin/nologin app \
    && mkdir -p /data/storage /data/tmp \
    && chown -R app:app /data \
    && chmod 0755 /app/scripts/api-entrypoint.sh

ENV PYTHONPATH=/app \
    VIDEO_FACTORY_DB=/data/video_factory.db \
    VIDEO_FACTORY_STORAGE=/data/storage \
    TMPDIR=/data/tmp

USER app

# Build-time proof that every module imports with the pinned deps
# (A2 F-26, staging VF-STG-01). Throwaway DB/storage, removed after.
RUN VIDEO_FACTORY_DB=/tmp/importcheck/vf.db \
    VIDEO_FACTORY_STORAGE=/tmp/importcheck/storage \
    TMPDIR=/tmp \
        python /app/scripts/import_check.py \
    && rm -rf /tmp/importcheck

VOLUME ["/data"]
EXPOSE 8000

# /healthz = liveness (no DB). /readyz is the readiness probe for ops.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# `--log-config` makes every stdout line JSON, including uvicorn's own
# startup / access / shutdown lines (docs/observability.md).
ENTRYPOINT ["/app/scripts/api-entrypoint.sh"]
# --proxy-headers trusts X-Forwarded-For/Proto only from
# FORWARDED_ALLOW_IPS (compose sets it to the compose network subnet;
# uvicorn's default is loopback only).
CMD ["uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-config", "api/uvicorn_log_config.json", \
     "--proxy-headers", \
     "--no-server-header"]
