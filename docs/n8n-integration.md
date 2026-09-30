# n8n Integration

The Video Factory is designed to be invoked from n8n (or any other
orchestrator) via webhook + polling. n8n is **not** required to run
V1; the integration is documented here as a contract.

## Triggers (n8n → Video Factory)

### Submit campaign request

```
POST $VIDEO_FACTORY_URL/api/campaigns
Content-Type: application/json

{
  "topic": "...",
  "audience": "...",
  "goal": "...",
  "target_duration_seconds": 30,
  "language": "en-US",
  "platforms": ["tiktok", "reels"],
  "count": 1,
  "render_profile": "mock",
  "approval_policy": "auto"
}
```

Response:

```json
{
  "campaign_id": "cmp_xxxxxxxxxxxx",
  "video_ids": ["vid_xxxxxxxxxxxx"],
  "pack_ids": ["vp_xxxxxxxxxxxxxxxx"]
}
```

### Manual review gate

If `approval_policy=needs_review`, the video pack arrives at
`NEEDS_REVIEW` state. n8n posts a webhook callback:

```
POST $VIDEO_FACTORY_URL/api/campaigns/{campaign_id}/videos/{video_id}/approve
Content-Type: application/json
{ "approved": true }
```

## Polling (n8n ← Video Factory)

The orchestrator exposes a status endpoint:

```
GET $VIDEO_FACTORY_URL/api/campaigns/{campaign_id}/videos/{video_id}
```

Response includes the current state machine position + outputs:

```json
{
  "state": "DONE",
  "outputs": [
    { "key": "mock/.../sc_a.mp4", "checksum": "...", "mime_type": "video/mp4" }
  ],
  "qc_ok": true,
  "assembled_ref": "assembled/.../stitched.mp4"
}
```

n8n polls every 5–30 seconds until `state` is terminal (`DONE`,
`FAILED`, or `CANCELLED`).

## Webhook callback (alternative)

For low-latency notifications, register a webhook at the campaign
submit endpoint:

```json
{ "callback_url": "https://n8n.example.com/webhook/video-factory" }
```

The Video Factory POSTs:

```json
{
  "campaign_id": "...",
  "video_id": "...",
  "state": "DONE",
  "final_outputs": [...]
}
```

If `HEYGEN_WEBHOOK_SECRET` is configured for the HeyGen direct path,
incoming webhooks are HMAC-verified before any state transition.
Webhook signature verification for the Video Factory's own webhooks
is not yet implemented in V1; rely on HTTPS + shared secret in the URL
for V1.

## Why n8n is not in the same loop as the state machine

Per prompt §4: "n8n — інтеграція/оркестрація, не сховище й не єдине
місце бізнес-логіки." The state machine, idempotency, and audit log
live in the Control Plane. n8n triggers work but never owns the truth.
This means:

- A failed n8n poll does not affect Video Factory state.
- A missed webhook does not crash the pipeline; the operator can
  re-poll and resume.
- The state machine is replayable from the audit log.

## Reference workflow (not auto-deployed)

A reference n8n workflow JSON is provided at
`workflows/n8n-video-factory-trigger.json.example` (planned; see
BLOCKERS.md B-005). Operators load it manually into their n8n.