# Roadmap

V0 ships a complete mock-only vertical slice. This document lists the
follow-up work needed to take the project from "demo on localhost" to
"production multi-company service". Each item links to the part of
the codebase it touches and the current state.

## Tier 1 — Blockers for any production use

These are the items that must land before V0 can serve real customers.

### 1. Authentication and authorization

**State:** V0 binds to `127.0.0.1` and has no auth layer.

**What is missing:**

- Bearer-token middleware on every `/api/v1/*` route.
- Per-company role checks (owner / editor / viewer / agent-service-account).
- Rate limiting per token (default 60 req/min).
- Audit log of who initiated what.

**Where it lands:** new `api/auth.py`, wired in `api/main.py:create_app()`.

**Cost:** ~1 day.

### 3. Reverse-proxy + TLS

**State:** README documents Caddy / nginx configs; nothing is shipped.

**What is missing:**

- Caddyfile for `https://video-factory.<domain>`.
- certbot / Let's Encrypt renewal cron.
- HSTS header, CSP, X-Frame-Options.

**Where it lands:** `ops/Caddyfile`, `ops/systemd/`.

**Cost:** ~half a day.

## Tier 2 — Real providers

### 4. Live HeyGen smoke test

**State:** `render_plane/heygen_direct.py` builds the request body
and then unconditionally raises `ProviderDisabled`.

**What is missing:**

- A separate, manual smoke test against a small-budget HeyGen org.
- After the smoke test passes: replace the `raise` with the
  commented-out `httpx.Client.post(...)` block.
- Webhook signature verification path (currently scaffolded but not
  exercised).
- HMAC secret rotation hook.

**Where it lands:** `render_plane/heygen_direct.py:113-125`.

**Cost:** ~1 day once a smoke-test account exists.

### 5. ComfyUI Cloud + GPU worker live paths

**State:** Both `comfy_cloud.py` and `comfy_gpu_worker.py` raise after
building the request. GPU teardown checks upload + checksum first.

**What is missing:**

- A self-hosted ComfyUI on a rented GPU VPS (RunPod / Vast.ai / Lambda).
- Wire `docs/operations-gpu.md` into a Docker Compose that boots
  ComfyUI + the API service.
- Bring-up runbook for `template_strategy.comfyui-gpu-worker`.
- Verified upload + checksum flow.

**Where it lands:** new `infra/comfyui-gpu-worker/`,
`docs/operations-gpu.md` becomes executable.

**Cost:** ~3 days; depends on a GPU account.

### 6. ComfyUI HeyGen Partner Nodes capability gate

**State:** The adapter calls `/object_info` to discover HeyGen
partner nodes. Not yet exercised against a real server.

**What is missing:**

- A test against a real Comfy server that exposes partner nodes.
- Capability flag persistence (`COMFY_HEYGEN_PARTNER_ENABLED`) tied
  to a verified `/object_info` snapshot.

**Where it lands:** `render_plane/comfy_heygen_partner.py:_object_info_heygen_nodes()`.

**Cost:** ~1 day once a real Comfy server is up.

### 7. Publer live publishing

**State:** V0 records publish intents. No external call.

**What is missing:**

- A Publer account with API access (Business plan minimum).
- A `PublerAdapter` against the existing `Publisher` protocol.
- Async job polling for the scheduled / published status.
- Caption per-platform templating (TikTok, IG, LinkedIn, X).

**Where it lands:** new `render_plane/publer_publisher.py`,
`api/main.py:create_publish_intent`.

**Cost:** ~2 days.

## Tier 3 — Robustness

### 8. PostgreSQL backend

**State:** V0 uses SQLite with WAL + per-thread connections. The
`core/repo.py` surface is small enough to swap.

**What is missing:**

- `core/repo_pg.py` mirroring the SQLite surface.
- A connection pool (asyncpg + aiopg).
- Migrations (alembic or hand-rolled).

**Where it lands:** `core/repo_pg.py`, factory in `api/main.py:create_app()`.

**Cost:** ~3 days.

### 9. S3 storage adapter

**State:** `LocalDiskStorage` is the only implementation. The
protocol is small.

**What is missing:**

- `S3Storage` with the same `put / get / exists / delete / list /
  path_for` interface.
- Multipart upload for large assets.
- Checksum + lifecycle policy.

**Where it lands:** `control_plane/storage.py`.

**Cost:** ~1 day.

### 10. Horizontal job runner

**State:** `JobRunner` is a process-local thread pool.

**What is missing:**

- A queue broker (Redis Streams, RabbitMQ, SQS, or NATS JetStream).
- Worker lease with heart-beat.
- Per-worker `IdempotencyStore` still durable.

**Where it lands:** new `core/runners_distributed.py`.

**Cost:** ~5 days.

### 11. Real n8n import + Telegram bot

**State:** `workflows/n8n-trigger.json` is structurally valid; runtime
import is not verified because no n8n instance exists in this
environment. Telegram bot is not built.

**What is missing:**

- Manual import + execution in a test n8n instance.
- Telegram bot that drives the API for brand onboarding + job
  submission (server side; the API contract is already shipped).

**Where it lands:** new `ops/n8n/`, `bot/`.

**Cost:** ~3 days.

## Tier 4 — UX

### 12. Real-time progress streaming

**State:** The UI polls `/jobs/{id}` every 1.5 s.

**What is missing:**

- WebSocket or SSE for live stage updates.
- Backend endpoint at `/api/v1/jobs/{id}/stream`.

**Where it lands:** `api/main.py`, `web/src/lib/api.js`.

**Cost:** ~1 day.

### 13. Job retry editor

**State:** `/jobs/{jid}/retry` re-runs the full pipeline.

**What is missing:**

- Per-stage retry (resume from a specific stage).
- Per-stage parameters override.

**Where it lands:** `core/runners.py:submit()`,
`api/main.py:retry_job()`.

**Cost:** ~2 days.

### 14. Visual workflow editor

**State:** Pipeline is defined in code; the UI shows it as a
read-only timeline.

**What is missing:**

- Drag-and-drop editor for the 12 stages.
- Persisted, versioned pipeline definitions per company.

**Where it lands:** new `web/src/routes/pipelines/`, persisted via
a `pipelines` table in `core/repo.py`.

**Cost:** ~5 days.

## Tier 5 — Quality

### 15. CI / CD

**State:** No CI exists. Tests run locally.

**What is missing:**

- GitHub Actions workflow: `pytest tests/`, `npm run check`, `npm run build`.
- Pre-commit hooks (identity-guard is already on `git commit`).

**Where it lands:** `.github/workflows/ci.yml`.

**Cost:** ~half a day.

### 16. Type safety on the API surface

**State:** V0 uses Pydantic only for FastAPI request bodies.

**What is missing:**

- Typed responses for every endpoint (`response_model=`).
- OpenAPI schema documented in CI artifact.

**Where it lands:** `api/main.py`.

**Cost:** ~1 day.

## How to propose a roadmap item

Open an issue using the template below. Tag with `roadmap` and the
relevant tier.

```markdown
## What
[One paragraph]

## Why
[Customer or operator pain]

## Where it lands
[Files / modules]

## Cost
[Engineering days, dependencies]
```

## Status legend

- **State:** current implementation status.
- **What is missing:** the gap.
- **Where it lands:** files to touch.
- **Cost:** rough engineering days, not including waiting on
  external accounts.

Items marked **BLOCKED** depend on an external account or platform
that is not available in this environment.