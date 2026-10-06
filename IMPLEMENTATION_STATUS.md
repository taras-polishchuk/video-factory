# Implementation Status

> Canonical source: `/home/taras/projects/.project-state/video-factory-production-2026-10-06/IMPLEMENTATION_STATUS.md`
> See mission-state canonical for the full status.

## Quick summary (post-production hardening 2026-10-06)

- Control Plane: complete, BudgetGuard actually invoked (audit §2.2 closed).
- Render Plane: MockRenderer end-to-end; live adapters behind ensure_live_unlocked
  + BudgetGuard preflight (audit §2.1 closed).
- Live OK gate: requires BOTH `LIVE_PROVIDER_TESTS=true` AND `SMOKE_TEST_PASSED=true`.
- Assembly: structured skip on all-mock OR sentinel video-when ffmpeg absent
  (audit §2.4 closed).
- Job pipeline: no longer fails when ffmpeg is missing; documents the skip
  and reaches DONE (audit §2.5 closed).
- Idempotency: durable failures surface to runner_audit, not silent pass
  (audit §2.8 closed).
- Publish intent: cross-company attack vector closed; platform names validated.
- Webhook: HeyGen handler with HMAC-SHA256 signature verification.
- Tests: 72/72 passing.
- Dockerfile: standalone, FFmpeg + non-root + HEALTHCHECK.
- start-local.sh: portable python selection + smoke check.

## Run

```bash
PYTHONPATH=. python3 -m pytest tests/ -q
python -m cli.video_factory --output-dir artifacts/demo
```