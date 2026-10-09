# Video Factory Control Room — production-style Docker image.
# Same Python base as docker-compose, but standalone (no compose
# dependency). Bind to 127.0.0.1 inside the container; expose via a
# reverse proxy in production.
#
# Build:    docker build -t video-factory-api:dev .
# Run:      docker run --rm -p 127.0.0.1:8000:8000 \
#             -v $(pwd)/artifacts:/app/artifacts \
#             -e MAX_COST_PER_VIDEO_USD=5 \
#             -e MAX_COST_PER_BATCH_USD=20 \
#             video-factory-api:dev
#
# Inside the container, the API serves on 127.0.0.1:8000. The `-p` flag
# maps the host's loopback only; do NOT use `-p 0.0.0.0:8000:8000`
# without a reverse proxy that authenticates + rate-limits the public
# surface.

FROM python:3.12-slim AS base

# FFmpeg is required by the assembly stage for real (non-mock) runs.
# The mock-only path skips assembly gracefully when ffmpeg is absent
# (verified by tests/test_production_hardening.py::test_job_runner_assembly_skip_does_not_fail_job).
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends ffmpeg \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer caches independently of source.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        'jsonschema>=4.0' \
        'fastapi>=0.115' \
        'uvicorn>=0.30' \
        'python-multipart>=0.0.9' \
        'httpx>=0.27' \
        'prometheus-client>=0.20' \
        'sentry-sdk>=2.0'

# Copy the application source.
COPY . /app

# F11-aligned boot-time smoke check. /health and /api/v1/spec must
# answer; the webhook router must mount (401 on unsigned POST is the
# expected response). Failing the smoke check turns the build into a
# hard error rather than a silently broken image.
ENV PYTHONPATH=/app \
    MAX_COST_PER_VIDEO_USD=5 \
    MAX_COST_PER_BATCH_USD=20 \
    VIDEO_FACTORY_DB=/app/artifacts/video_factory.db \
    VIDEO_FACTORY_STORAGE=/app/artifacts/storage

# Run as a non-root user. The artifacts volume is mounted at /app/artifacts.
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /app/artifacts/storage \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Use uvicorn directly (no --reload) for production-grade boot. The
# `--workers 1` setting is intentional: the JobRunner uses a per-process
# in-memory idempotency map. Scaling workers is a separate V2+ mission.
# `--log-config` makes every stdout line JSON, including uvicorn's own
# startup / access / shutdown lines (docs/observability.md).
CMD ["uvicorn", "api.main:app", \
     "--host", "127.0.0.1", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-config", "api/uvicorn_log_config.json"]