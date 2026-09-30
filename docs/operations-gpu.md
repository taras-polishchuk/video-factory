# GPU Worker Operations Runbook

This document describes the manual operations for the Comfy GPU
worker render profile. V1 ships the contract; this runbook covers
the actual bring-up, smoke test, batch, and teardown sequence.

## Bring-up

1. **Provision a GPU host.** A rented VPS (RunPod, Vast.ai, Lambda,
   Hetzner, etc.) is the typical choice. Pick a GPU large enough for
   the model(s) you intend to load. Comfy UI supports most Stable
   Diffusion checkpoints plus video-specific models (Wan, AnimateDiff,
   etc.).

2. **Install ComfyUI + models.** Follow the official Comfy install
   instructions. Verify model checkpoints + VAE + CLIP are present.

3. **Expose Comfy via HTTPS.** Either:
   - Terminate TLS at a reverse proxy (Caddy, nginx) on the host, OR
   - Use a Tailscale Funnel / Cloudflare Tunnel to expose the host's
     `:8188` over HTTPS with auth.
   The ComfyUI server **must not** be exposed without TLS and auth.
   Default Comfy has no auth.

4. **Configure secrets in the Video Factory host.** Set
   `COMFY_GPU_API_BASE`, `COMFY_GPU_USER`, `COMFY_GPU_PASSWORD`,
   `LIVE_PROVIDER_TESTS=true`, `MAX_COST_PER_BATCH_USD=<ceil>`.

5. **Health check.** From the Video Factory host:
   ```bash
   curl -fsS -u "$COMFY_GPU_USER:$COMFY_GPU_PASSWORD" \
     "$COMFY_GPU_API_BASE/system_stats" | jq .
   ```
   Expect 200 OK with GPU + model info.

## Pre-flight

1. **Verify required models are loaded.** `/object_info` enumerates
   node types and inputs; a video workflow's `CheckpointLoaderSimple`
   must reference a known checkpoint.
2. **Smoke test.** Submit one scene (low `takes_per_scene=1`,
   `quality_preset=draft`) and wait for output. Confirm:
   - `provider_job_id` returned.
   - status transitions PENDING → PROCESSING → COMPLETED.
   - `fetch_outputs()` returns a non-empty `OutputAsset` list.
   - The asset's `ref` exists in your storage layer.
   - The asset's `checksum` matches what was actually written.

## Batch run

1. Submit the campaign with the configured `render_profile=comfy_gpu_worker`.
2. The orchestrator enqueues per-scene jobs; the worker drains the
   queue. ComfyUI processes one prompt at a time per server.
3. For each completed output, the orchestrator records `verify_upload`
   with the storage layer (the storage layer is responsible for
   verifying the bytes + checksum before declaring success).
4. After every output is verified, the orchestrator transitions
   `ASSEMBLING → DONE`.

## Teardown

GPU teardown is **gated**, not automatic. The adapter refuses to
issue `/free` + `/queue/clear` + VPS destroy until:

1. Every `provider_job_id`'s outputs have a recorded `verify_upload`
   call.
2. The queue is empty.
3. `COMFY_GPU_ALLOW_ACTIONS=true`.
4. `destroy_after_batch=true` was passed at submission.

If any check fails, the adapter raises `TeardownUnsupported` and the
operator must run the manual teardown:

1. Stop the orchestrator to prevent further submits.
2. SSH into the GPU host and stop the Comfy process:
   ```bash
   systemctl stop comfyui  # or docker compose stop
   ```
3. Verify all outputs are still in your storage (no orphan artifacts).
4. Destroy the VPS via the provider dashboard.

## Cost discipline

- Per-render cost is **unknown** in V1; verify on the GPU provider
  dashboard before live runs.
- `MAX_COST_PER_BATCH_USD` is a hard ceiling enforced by
  `BudgetGuard.preflight()`.
- `BudgetGuard.record_spend()` is called by the orchestrator after
  each verified output.

## What this runbook does NOT cover

- GPU-specific model selection (depends on workflow).
- Workflow authoring (see `workflows/`).
- Storage adapter configuration for S3 / GCS / R2 (out of scope for
  V1; LocalDiskStorage is the default).

## When docs change

ComfyUI's API surface evolves. Before each major version bump,
re-verify:
- `POST /prompt` request/response shape.
- `GET /history/{id}` output descriptor shape.
- `/object_info` node type names.

The verified doc URL is:
https://docs.comfy.org/development/comfyui-server/api-examples