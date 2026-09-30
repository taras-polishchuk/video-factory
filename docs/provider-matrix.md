# Provider Matrix

This matrix enumerates every render profile the Video Factory knows
about in V1, with its auth owner, billing owner, output kinds, secret
requirements, and known limitations. Live rows are contract-only in
V1 — they never auto-execute.

## Verification status (verified 2026-09-30)

| Doc URL | Status |
|---|---|
| https://developers.heygen.com/ | Verified |
| https://developers.heygen.com/cli | Verified |
| https://developers.heygen.com/skills/overview | Verified |
| https://developers.heygen.com/webhooks | Verified |
| https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_api_nodes/nodes_heygen.py | Verified |
| https://docs.comfy.org/development/comfyui-server/api-examples | Verified |
| https://docs.comfy.org/tutorials/partner-nodes/openai/dall-e-3 | Verified |
| https://comfy.org/pricing/ | Verified |
| https://support.comfy.org/articles/2703236295-comfy-api-deploy-your-comfyui-workflow-as-an-api | Verified |

Always re-check the docs before billing is enabled. Pricing and node
availability change without notice.

## Matrix

| Profile | Auth owner | Billing owner | Output kinds | Secret required | Status |
|---|---|---|---|---|---|
| `mock` | n/a | `offline` | scene_clips, whole_video, tts | no | verified offline |
| `heygen_direct` | `HEYGEN_API_KEY` server-side | HeyGen account | whole_video, avatar_video | yes | contract-only |
| `comfy_cloud` | `COMFY_CLOUD_API_KEY` server-side | Comfy Cloud account | scene_clips, translation | yes | contract-only |
| `comfy_gpu_worker` | `COMFY_GPU_*` server-side | tenant (rented VPS) | scene_clips | yes | contract-only |
| `comfy_heygen_partner` | Comfy proxy auth (NOT user HeyGen key) | Comfy Cloud account | avatar_video, translation, tts | no (Comfy owns auth) | capability-gated |

## Output kinds

- `whole_video` — single finished video (no scene stitching required).
  HeyGen direct Video Agent path.
- `avatar_video` — avatar-led video. HeyGen Avatar API or Comfy HeyGen
  Partner Node.
- `scene_clips` — one clip per scene. Comfy GPU worker / Comfy Cloud.
  Requires FFmpeg stitch.
- `translation` — localized variants. Comfy Cloud or Comfy HeyGen
  Partner Node.
- `tts` — voice track only. Comfy HeyGen Partner Node.

## BYOK status (verified 2026-09-30)

- **HeyGen API key via Comfy Partner Nodes:** Not a documented BYOK path
  in the verified public docs. Partner nodes invoke HeyGen via the
  Comfy proxy; we do not pass the user's own HeyGen API key to them.
  Verified BYOK docs list Runway and Gemini for Comfy; HeyGen is
  Enterprise contact. We document this and do not claim BYOK support.
- **HeyGen API key via HeyGen direct REST:** BYOK via the user's own
  HeyGen account. Standard server-side credential. We implement this
  path (contract only) but do not auto-execute it.

## Per-render safety gates

1. `LIVE_PROVIDER_TESTS=true` must be set in env.
2. `MAX_COST_PER_BATCH_USD > 0` must be configured.
3. The `BudgetGuard.preflight()` must return `allowed=True` before
   `submit()` is called.
4. Idempotency key is computed from `content_hash`; retries replay
   the prior receipt.
5. Webhook signature validation (when `HEYGEN_WEBHOOK_SECRET` is set)
   runs before any state transition is honored.

If any gate fails, the live adapter raises `ProviderDisabled` (a
configuration error) or `BudgetExceeded` (a preflight failure).

## GPU teardown gate (Comfy GPU worker)

Per `render_plane.comfy_gpu_worker.ComfyGPUWorkerAdapter.teardown_gpu`:

1. Every output must have a `verify_upload(provider_job_id, output_ref)`
   call recorded by the operator (or by the storage layer).
2. The queue for the batch must be empty.
3. `COMFY_GPU_ALLOW_ACTIONS=true` must be set.
4. `destroy_after_batch=true` must be passed at batch submission.

If any check fails, `TeardownUnsupported` is raised. The Comfy server
is left running; the runbook (`docs/operations-gpu.md`) describes the
manual teardown steps.