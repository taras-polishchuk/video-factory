# Architecture — Video Factory Control Room (V0)

The original V1 architecture (Control Plane + Render Plane + FFmpeg
assembly) is unchanged. V0 adds:

- A multi-company **domain layer** (`core/`) wrapping the existing
  Control Plane behind SQLite-backed persistence.
- A thin **HTTP API** (`api/`) for the UI, Telegram agent, and n8n.
- A **background job runner** that drives the orchestrator off the
  HTTP request thread.
- A **web UI** (`web/`) over the API. SvelteKit.

## Layered view

```
┌─────────────── BROWSER (SvelteKit) ────────────────┐
│  Workspace / Company Bible / Assets /              │
│  Create video / Jobs list / Job detail /           │
│  Publishing / Integrations                         │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP / JSON
                       ▼
┌─────────────── HTTP API (FastAPI) ─────────────────┐
│  /api/v1/companies · bibles · assets · jobs ·     │
│  publish · integrations · health                   │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────── CONTROL PLANE (core + existing) ────┐
│  Repo (SQLite, per-thread connections)             │
│  JobRunner (thread pool, durable idempotency,      │
│             BudgetGuard preflight, EditPlan stage) │
│  BudgetEnforcer · EditPlan · FFmpeg assembly       │
│  Existing: state machine · audit · idempotency     │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────── RENDER PLANE ────────────────────────┐
│  MockRenderer (offline, generates playable MP4)     │
│  Live adapters (HeyGen, ComfyUI x3) — contract-    │
│  only, raise ProviderDisabled without a verified   │
│  smoke test                                       │
└─────────────────────────────────────────────────────┘
```

## Multi-company model

Every row in `core/repo.py` carries `company_id`. The API enforces
isolation; the repo exposes `Forbidden` on cross-company access.
Same-product, different-company is the deployment model: one code,
many workspaces.

```
companies ─< bible_versions
         ─< assets
         ─< jobs ─< job_stages
                ─< job_outputs
                ─< publish_intents
integrations (singleton table; provider state)
idem_receipts (singleton; content-derived idempotency)
```

## Job pipeline

The `JobRunner` records 12 ordered stages per job. Stages 1–5 are
synthetic planning (mock-mode placeholder; replace with real LLM
stages later). Stages 6–12 cover generation, edit, QC, review,
export. Each stage records `started_at`, `finished_at`, `duration_ms`,
status (`pending|running|done|failed|skipped`), and a `summary_json`.

## Edit stage

`core/edit_plan.py` produces an `EditPlan` (clip order, durations,
caption lines, overlay). `render_plan_to_storage()` executes it
deterministically with FFmpeg — produces a real playable MP4 in
mock mode. The plan is fingerprint-stable so retries can be diffed.

## Safety gates

1. **Localhost only.** Default `127.0.0.1`. No public exposure
   without adding auth + TLS + reverse-proxy.
2. **Mock-only by default.** Integrations are seeded with
   `enabled=false, mode=mock`. Live adapters raise
   `ProviderDisabled` after the env gate. No env var combination
   enables live requests in V0.
3. **BudgetGuard preflight.** Mock renderer reports `amount_usd=0.0,
   unknown=False` so the gate passes with non-zero caps. Live
   adapters report `unknown=True` and are refused at preflight.
4. **Durable idempotency.** Receipts live in a SQLite file under
   `storage_root/idem.db`. A retry on the same content hash reuses
   the receipt after a process restart.
5. **File uploads.** 50 MB cap. Path-traversal filenames rejected.
   Storage root is fixed; assets are scoped per-company.
6. **Cross-company isolation.** Tested with `test_cross_company_bible_denied`.

## API ↔ agent boundary

The HTTP API is the single integration surface for the UI, the
Telegram agent, and n8n. See `docs/agent-integration.md`.

The Python control plane remains the source of truth for state
machine, idempotency, and audit. n8n is the courier; the Telegram
agent is one front-end; the web UI is another.

## What is intentionally NOT in V0

- Live HeyGen, ComfyUI Cloud, ComfyUI GPU worker, ComfyUI HeyGen
  Partner Nodes — contract-only, no HTTP path enabled.
- Live Publer publishing — accepted intents are recorded but never
  posted to Publer.
- Real authentication or authorization.
- Multi-host deployment topology (this V0 binds one process per
  workspace group).
- WebSocket progress streaming (polling is sufficient at V0 cadence).