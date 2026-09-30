# Architecture

## Control Plane vs Render Plane

The Control Plane owns campaign state — intake validation, video pack
manifests, state machine transitions, queue + lease, budget guard, audit
log, and idempotency. It runs anywhere Python can run; no GPU required.

The Render Plane is the swappable backend that produces video assets
based on a VideoPack. Every adapter conforms to a single Renderer
protocol so the Control Plane never branches on the active profile.

```
┌─────────── CONTROL PLANE ───────────┐
│ CampaignRequest                      │
│   ↓                                  │
│ IntakeValidator (rejects bad input)  │
│   ↓                                  │
│ PackBuilder (schema-validated        │
│   VideoPack; content-derived pack_id)│
│   ↓                                  │
│ VideoStateMachine                    │
│   DRAFT→RESEARCHING→PREPARING→       │
│   READY_FOR_RENDER→QUEUED→           │
│   SUBMITTING→PROCESSING→OUTPUTS_READY│
│   →QC→ASSEMBLING→DONE               │
│   ↓                                  │
│ Orchestrator                         │
│   • BudgetGuard                      │
│   • IdempotencyStore                 │
│   • Queue + Lease                    │
│   • AuditLog                         │
│   • Storage                          │
└──────────┬───────────────────────────┘
           │ pack_dict + idempotency_key
           ▼
┌─────────── RENDER PLANE ────────────┐
│ Renderer protocol                   │
│   capabilities / estimate /          │
│   submit / status / fetch_outputs /  │
│   cancel / teardown_gpu             │
│                                       │
│ Adapters:                             │
│   • MockRenderer (offline)            │
│   • HeyGenDirectAdapter               │
│   • ComfyCloudAdapter                 │
│   • ComfyGPUWorkerAdapter             │
│   • ComfyHeyGenPartnerAdapter         │
└──────────┬───────────────────────────┘
           │ OutputAssets (refs + checksums)
           ▼
┌─────────── ASSEMBLY + QC ───────────┐
│ FFmpeg scene-clip stitch             │
│ FFprobe container/duration/audio QC  │
│ Caption timing check                 │
│ Mock-detection via sentinel header    │
└──────────────────────────────────────┘
```

## State machine

Explicit transition graph. No silent relaxation.

| From | Allowed targets |
|---|---|
| DRAFT | RESEARCHING, CANCELLED |
| RESEARCHING | PREPARING, FAILED, CANCELLED |
| PREPARING | NEEDS_REVIEW, READY_FOR_RENDER, FAILED, CANCELLED |
| NEEDS_REVIEW | READY_FOR_RENDER, PREPARING, CANCELLED |
| READY_FOR_RENDER | QUEUED, CANCELLED |
| QUEUED | SUBMITTING, CANCELLED |
| SUBMITTING | PROCESSING, FAILED, QUEUED, CANCELLED |
| PROCESSING | OUTPUTS_READY, FAILED, CANCELLED |
| OUTPUTS_READY | QC, FAILED |
| QC | ASSEMBLING, FAILED |
| ASSEMBLING | DONE, FAILED |
| DONE | (terminal) |
| FAILED | DRAFT |
| CANCELLED | (terminal) |

Every transition is recorded in the audit log. Invalid transitions
raise `InvalidStateTransition`.

## Idempotency

Idempotency keys are content-derived:

```
idem_key = sha256(campaign_id + video_id + render_profile + content_hash)[:16]
```

A retry sees the same `idem_key` and replays the prior receipt without
re-invoking the provider. `campaign_id` and `video_id` themselves are
derived from `content_hash` to make retries converge on identical IDs.

The orchestrator owns the canonical `IdempotencyStore`. Live adapters
that maintain their own store (e.g. for cancellation tracking) are
wired via `set_idempotency_store()` so retries hit the same map.

## Output normalization

Each renderer declares a `Capabilities` object with the output kinds it
can produce:

- `whole_video` — single finished video; assembly is skipped.
- `avatar_video` — avatar-led video; can be combined with B-roll.
- `scene_clips` — one clip per scene; FFmpeg stitch required.
- `translation` — localized variants of an existing asset.
- `tts` — voice track only.

The orchestrator reads `output_kind` from the VideoPack's
`render_capabilities` and selects the right postprocessor path.

## Mock detection

Mock fixtures carry a sentinel header (`MOCKRENDERv1\x00\x00`) so
downstream consumers can tell them apart from real containers. QC
explicitly labels them as `mock_marker:<path>` rather than failing
on ffprobe decode errors. Production renderers must NOT emit this
header; their output is checked against real-container expectations.

## Sequence (offline demo)

```
1. CLI / test calls orchestrator.run_to_done(request, ...)
2. Orchestrator.prepare_pack()
   a. IntakeValidator validates request, resolves platforms/aspects
   b. PackBuilder builds VideoPack (content-derived pack_id)
   c. VideoStateMachine starts at DRAFT, advances to READY_FOR_RENDER
3. Orchestrator.run_to_done() main path
   a. State → QUEUED → SUBMITTING
   b. IdempotencyStore.lookup(idem_key)
      - miss: renderer.submit(pack_dict, idem_key); store.save(receipt)
      - hit:  replay receipt; record submit.replay audit event
   c. State → PROCESSING → poll status until terminal
   d. fetch_outputs() returns OutputAsset descriptors
   e. _persist_asset() points at storage keys without copying
   f. qc_outputs() runs ffprobe + caption timing check
   g. State → QC → ASSEMBLING (scene_clips path)
      - stitch_scene_clips() runs FFmpeg concat + audio mux
      - FFmpeg unavailable: assembly.skipped audit event
   h. State → DONE
4. CLI writes report.json + video-pack.json to artifacts/demo/
```

## Sequence (live provider, opt-in)

The same path runs against a live adapter once
`LIVE_PROVIDER_TESTS=true` AND `MAX_COST_PER_BATCH_USD > 0` are set.
Live adapters raise `ProviderDisabled` otherwise. Live calls go through
the same idempotency layer; retried webhook deliveries converge on
the same `provider_job_id`.