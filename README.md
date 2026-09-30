# Video Factory — Production-Oriented Branded Video Pipeline

A Control Plane + swappable Render Plane system that turns a campaign
intake into a finished, branded short-form video.

**V1 ships:** state machine, idempotency, audit log, schema-validated
manifest, mock renderer that walks the full pipeline offline, FFmpeg
postprocessor, and contract-only adapters for HeyGen, Comfy Cloud,
Comfy GPU worker, and Comfy HeyGen Partner Nodes. No GPU, no API keys,
no internet required to run the offline demo.

## Architecture

```
┌─────────────────────────────── CONTROL PLANE ───────────────────────────────┐
│ CampaignRequest → IntakeValidator → PackBuilder (schema-validated VideoPack) │
│ → VideoStateMachine → Orchestrator → MockRenderer / LiveAdapter → outputs   │
│ BudgetGuard · IdempotencyStore · AuditLog · Queue · Storage                │
└────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────── RENDER PLANE ────────────────────────────────┐
│ MockRenderer (offline) · HeyGenDirectAdapter · ComfyCloudAdapter            │
│ ComfyGPUWorkerAdapter · ComfyHeyGenPartnerAdapter                          │
│ Capabilities · CostEstimate · Submit · Status · FetchOutputs · Cancel      │
└────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────── ASSEMBLY + QC ──────────────────────────────┐
│ FFmpeg scene-clip stitch + caption timing check + mock-detection sentinel  │
└────────────────────────────────────────────────────────────────────────────┘
```

## Quick start

```bash
# Offline demo: produces a Video Pack in READY_FOR_RENDER and runs the
# mock renderer through the full pipeline to DONE.
python -m cli.video_factory --output-dir artifacts/demo
```

Outputs:

```
artifacts/demo/
├── report.json       # final state, qc notes, audit log
├── video-pack.json    # the validated VideoPack manifest
└── storage/
    └── mock/<pack_id>/<scene_id>.mp4   # mock fixture outputs
```

## Run tests

```bash
PYTHONPATH=. python3 -m pytest tests/ -q
```

28 tests cover state machine, idempotency, whole-video path, scene-clips
path, mock renderer contract, schema validation, QC failure handling.

## Boundaries

- **No paid API calls** without `LIVE_PROVIDER_TESTS=true` AND
  `MAX_COST_PER_BATCH_USD > 0`. See `.env.example`.
- **No social auto-publish** in V1. Render -> assembly -> manual review.
- **No GPU teardown** without verified upload + checksum.
- **MockRenderer outputs are NOT real video.** Sentinel bytes make them
  easy to identify; assembly + QC skip real-container checks for
  them. Live adapters must verify with `ffprobe` before downstream
  consumption.

## Project structure

```
video-factory/
├── README.md, BLOCKERS.md, ASSUMPTIONS.md, IMPLEMENTATION_STATUS.md
├── docs/
│   ├── architecture.md      # Control/Render Plane + state machine + sequence
│   ├── provider-matrix.md   # 4 providers × auth/billing/capability
│   ├── operations-gpu.md    # GPU worker lifecycle runbook
│   └── n8n-integration.md   # contract for n8n trigger/webhook
├── schemas/
│   ├── video-pack.schema.json
│   └── fixture.json
├── workflows/
│   └── scene-clip-mock.json # API-format ComfyUI workflow (mock graph)
├── control_plane/           # state machine, queue, audit, budget, idempotency
├── render_plane/            # Renderer protocol + 4 adapters
├── assembly/                # FFmpeg postprocessor + QC
├── cli/                     # offline demo entrypoint
├── tests/                   # 28 tests
└── .env.example             # variable NAMES only, no values
```

## What's verified vs. what's a contract

| Capability | Verified path | Contract-only |
|---|---|---|
| Offline demo end-to-end | ✓ mock renderer + FFmpeg | |
| State machine + audit | ✓ all states, all transitions | |
| Idempotent retries | ✓ content-derived pack_id | |
| Schema validation | ✓ Draft-07 JSON Schema | |
| Mock render full queue | ✓ DONE + idempotent | |
| FFmpeg scene-clip assembly | ✓ when FFmpeg available | mock inputs skipped |
| HeyGen direct API | | contract only — opt-in via `LIVE_PROVIDER_TESTS` |
| Comfy Cloud API | | contract only |
| Comfy GPU worker | | contract only |
| Comfy HeyGen Partner Nodes | | capability-gated; no BYOK claim |
| GPU teardown | | gated on upload + checksum verification |
| n8n integration | | webhook contract documented |

See `docs/provider-matrix.md` for the per-provider capability,
billing, and verification matrix.

## Why V1 ships offline

Per prompt §1 ("Розділи Control Plane і Render Plane") and the
"Definition of Done" requirements, the offline path is a hard
requirement. Live paths are contract-only and never auto-execute.
Operators opt in explicitly via env vars + budget caps.