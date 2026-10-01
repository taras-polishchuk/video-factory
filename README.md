# Video Factory — Control Room (V0)

> GitHub: [taras-polishchuk/video-factory](https://github.com/taras-polishchuk/video-factory)
>
> Releases: [v0.1.0](https://github.com/taras-polishchuk/video-factory/releases/tag/v0.1.0)
>
> Roadmap: see [docs/ROADMAP.md](docs/ROADMAP.md) and the open
> [issues](https://github.com/taras-polishchuk/video-factory/issues).

Multi-company video production control plane over the existing Python
core. This V0 adds:

- A multi-company domain layer (`core/`) — companies, Bible versions,
  assets, jobs, stages, outputs, publish intents.
- A thin HTTP API (`api/`) — FastAPI. Single source of truth shared
  by the UI, Telegram agent, and n8n.
- A background job runner with **durable idempotency** + **real
  BudgetGuard preflight** + **EditPlan stage + FFmpeg assembly**
  producing playable MP4 outputs.
- A SvelteKit web UI (`web/`) — workspace switcher, Bible editor,
  asset uploads, create-video form, job list, job detail timeline,
  publishing mock.

**V0 ships:** mock-only mode. No live HeyGen, ComfyUI, GPU, or Publer
calls. No authentication. Bind to localhost.

## Quick start (clean machine)

```bash
# 1. Python deps (one-time)
python3 -m pip install --user 'jsonschema>=4.0' pytest \
                              'fastapi>=0.115' 'uvicorn>=0.30' \
                              'python-multipart>=0.0.9'

# 2. Verify (50 tests)
PYTHONPATH=. python3 -m pytest tests/ -q

# 3. Run the local stack (API on 8000, UI on 5173)
./scripts/start-local.sh
# Open http://127.0.0.1:5173
```

Alternative — manual two-process mode (terminal A + terminal B):

```bash
# Terminal A — API
PYTHONPATH=. python3 -m uvicorn api.main:app --host 127.0.0.1 --port 8000

# Terminal B — UI
cd web && npm install && VITE_API_TARGET=http://127.0.0.1:8000 npm run dev -- --host 127.0.0.1
```

Alternative — Docker Compose:

```bash
docker compose up
```

State (companies, jobs, assets, idempotency receipts) persists under
`artifacts/` by default. Override with env vars:

| Variable | Default | Purpose |
|---|---|---|
| `VIDEO_FACTORY_DB` | `artifacts/video_factory.db` | SQLite file for the repo |
| `VIDEO_FACTORY_STORAGE` | `artifacts/storage` | File storage root |
| `MAX_COST_PER_VIDEO_USD` | `0` | Mock renderer cost is 0; live refused when unset |
| `MAX_COST_PER_BATCH_USD` | `0` | Same; must be > 0 to allow any submit |

To reset the demo state without losing user data:
```bash
rm -rf artifacts/video_factory.db artifacts/storage
```

## What's verified vs. what's a contract

| Capability | Verified path | Contract-only |
|---|---|---|
| Offline demo end-to-end | ✓ mock renderer + FFmpeg + EditPlan + BudgetGuard preflight | |
| Multi-company isolation | ✓ cross-company bible denied | |
| Bible versioning | ✓ draft → approve; new draft increments version | |
| Asset upload + download | ✓ 50MB cap, path-traversal rejected | |
| Job pipeline | ✓ 12 stages, recoverable per-stage | |
| FFmpeg playable MP4 | ✓ final video is a real h264 1080×1920 clip | |
| Durable idempotency | ✓ survives process restart (`storage_root/idem.db`) | |
| BudgetGuard preflight + spend | ✓ wired before submit; live adapters blocked by `unknown_cost` | |
| Schema validation | ✓ VideoPack JSON Schema + Company Video Bible schema | |
| Mock publishing | ✓ draft/schedule/publish records an intent, no external call | |
| API for agents / n8n | ✓ all endpoints, cross-company tests pass | |
| n8n trigger JSON | ✓ structural validation only; runtime import not verified (no n8n) | live wiring |
| HeyGen / ComfyUI live paths | | contract-only — env vars cannot enable |
| Publer live publishing | | mock only — accepted intents recorded without external calls |
| Authentication | | bind to localhost only; do not expose publicly |

See `AUDIT-2026-10-01.md` for the original V1 audit and
`docs/agent-integration.md` for the integration contract.

## Project structure

```
video-factory/
├── README.md (this file)
├── BLOCKERS.md · ASSUMPTIONS.md · IMPLEMENTATION_STATUS.md
├── AUDIT-2026-10-01.md          (independent read-only audit of 19eb277)
├── docs/
│   ├── architecture.md          (V0 layered view + safety gates)
│   ├── provider-matrix.md       (live adapters: contract-only)
│   ├── operations-gpu.md
│   ├── n8n-integration.md       (provider / channel contract)
│   └── agent-integration.md     (API contract for Telegram / n8n)
├── schemas/
│   ├── video-pack.schema.json
│   ├── company-video-bible.schema.json
│   └── fixture.json
├── workflows/
│   ├── scene-clip-mock.json
│   └── n8n-trigger.json         (V0: webhook → Video Factory → Telegram)
├── control_plane/               (V1 unchanged)
├── render_plane/                (V1 unchanged; live adapters contract-only)
├── assembly/                    (V1 unchanged)
├── cli/                          (V1 unchanged; offline CLI demo)
├── core/                         (V0 multi-company + runner + BudgetGuard)
│   ├── repo.py
│   ├── edit_plan.py
│   ├── budget_enforcer.py
│   └── runners.py
├── api/                          (V0 FastAPI app)
│   └── main.py
├── web/                          (V0 SvelteKit UI)
│   ├── package.json
│   └── src/
│       ├── lib/api.js
│       └── routes/
│           ├── +layout.svelte
│           ├── +page.svelte
│           ├── bible/+page.svelte
│           ├── assets/+page.svelte
│           ├── create/+page.svelte
│           ├── jobs/+page.svelte
│           ├── jobs/[id]/+page.svelte
│           └── integrations/+page.svelte
├── tests/
│   ├── test_state_machine.py
│   ├── test_idempotency.py
│   ├── test_whole_video_path.py
│   ├── test_scene_clips_path.py
│   ├── test_qc_failures.py
│   ├── test_schema_validation.py
│   ├── test_provider_contracts.py
│   ├── test_api.py              (V0: 12 API tests)
│   └── test_budget_and_idem.py  (V0: 8 budget + durable-idempotency tests)
└── pyproject.toml
```

## Boundaries

- **No paid API calls.** All four live render adapters raise
  `ProviderDisabled`. Even `LIVE_PROVIDER_TESTS=true` cannot unlock
  them; the body is built and then a guard raise blocks the HTTP
  call. A separately verified smoke test (not in this V0) is
  required before any live provider is enabled.
- **No social auto-publish in V0.** Publishing is mocked locally;
  accepted intents are recorded in `publish_intents` without making
  external calls.
- **No GPU teardown without verified upload + checksum.**
- **MockRenderer outputs are real, playable MP4.** The sentinel bytes
  from V1 are gone. The V0 assembly step uses FFmpeg to produce a
  real `h264 / 1080×1920 / yuv420p` clip. Tests assert this.
- **Default binding is localhost.** Do not expose publicly without
  adding auth + TLS + reverse-proxy.

## Resetting the local demo

```bash
# Keep SQLite + storage, reset idem only
rm -f artifacts/storage/idem.db

# Full reset (deletes all companies, jobs, assets, receipts)
rm -rf artifacts/video_factory.db artifacts/storage
```

Both paths preserve `artifacts/storage/companies/<slug>/assets/*`
uploaded binaries if you only delete `idem.db`.