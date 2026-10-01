# Video Factory Control Room — Agent + n8n Integration

This document is the contract for any external system (Telegram agent,
n8n workflow, custom integration) that wants to drive Video Factory.

The Video Factory API is the **only** source of truth for company,
Bible, asset, job, stage, output, and publishing state. Anything that
calls into it shares the same models and the same audit log.

## Base URL

Local development:

```
http://127.0.0.1:8000
```

Production deployment is out of scope for V0 and must add auth +
TLS before exposing the panel publicly.

## Authentication

V0 binds to `127.0.0.1` and assumes a single local operator. There is
no authentication on the API. **Do not expose the API publicly without
adding auth + TLS first.** See the README for the deployment warning.

## API surface

All paths are prefixed with `/api/v1`. Bodies are JSON unless stated.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + ffmpeg + mock flag |
| GET | `/companies` | List workspaces |
| POST | `/companies` | Create `{slug, name, description?}` |
| GET | `/companies/{cid}` | Profile |
| PATCH | `/companies/{cid}` | Update name / description |
| GET | `/companies/{cid}/bibles` | List versions |
| POST | `/companies/{cid}/bibles` | Create draft `{payload, notes?}` |
| GET | `/bibles/{bid}` | Get one |
| POST | `/bibles/{bid}/approve` | Lock a draft |
| GET | `/companies/{cid}/assets` | List assets |
| POST | `/companies/{cid}/assets?kind=logo` | Multipart upload |
| GET | `/assets/{aid}/download` | Download bytes |
| GET | `/jobs?company_id=&state=` | Filter |
| POST | `/jobs` | Create `{company_id, bible_id?, topic, language, duration_seconds, platforms, aspect_ratio?, count?, render_profile?, approval_required?}` |
| GET | `/jobs/{jid}` | State + status_message + current_stage |
| GET | `/jobs/{jid}/stages` | Per-stage execution history |
| GET | `/jobs/{jid}/outputs` | Output metadata |
| POST | `/jobs/{jid}/retry` | Re-run from current state |
| GET | `/jobs/{jid}/outputs/{oid}/download` | Download |
| POST | `/jobs/{jid}/publish` | `{action: draft|schedule|publish, platforms, caption?, scheduled_for?}` |
| GET | `/jobs/{jid}/publish` | List publish intents |
| GET | `/integrations` | Provider connection state |

## Live example (cURL)

```bash
# 1. Create a workspace
curl -s -X POST http://127.0.0.1:8000/api/v1/companies \
  -H 'Content-Type: application/json' \
  -d '{"slug":"acme","name":"Acme Co."}'
# {"company_id":"cmp_...","slug":"acme",...}

# 2. Create a Bible draft
curl -s -X POST http://127.0.0.1:8000/api/v1/companies/cmp_xxx/bibles \
  -H 'Content-Type: application/json' \
  -d '{"payload":{"name":"Acme","audience":"devs","colors":{"primary":"#0d9488"}}}'
# {"bible_id":"bbl_xxx","status":"draft",...}

# 3. Approve
curl -s -X POST http://127.0.0.1:8000/api/v1/bibles/bbl_xxx/approve

# 4. Create a job
curl -s -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"company_id":"cmp_xxx","bible_id":"bbl_xxx","topic":"Hello",
       "language":"en-US","duration_seconds":15,"platforms":["tiktok"]}'
# {"job_id":"job_xxx","state":"queued",...}

# 5. Poll until done
curl -s http://127.0.0.1:8000/api/v1/jobs/job_xxx

# 6. Download final video
curl -s -o out.mp4 http://127.0.0.1:8000/api/v1/jobs/job_xxx/outputs/<oid>/download
```

## n8n workflow

`workflows/n8n-trigger.json` is a structurally-validated reference
workflow. It accepts a webhook, normalizes the payload, calls the
Video Factory API, branches on success, and forwards the result to a
Telegram bot.

JSON structure was validated locally; runtime import into a live n8n
instance is **not** verified because no n8n is available in this
environment. Import and exercise it manually when you have an n8n
up.

## Telegram agent — sample flow

A human-facing Telegram bot can drive the API with this conversation
shape. No UI is required for these steps.

1. User: `/new-video`
2. Bot: `Which company?` → user picks or `/create`.
3. Bot: `Approve a new Bible, or use the latest approved version?`
4. If new: bot collects logo / PDF / links / examples and POSTs to
   `/api/v1/companies/{cid}/bibles` with a draft payload. The bot must
   NOT pretend to extract values with AI unless a real extraction
   provider is implemented; mock mode can offer a labelled sample
   proposal.
5. Bot: `Approve?` → user confirms → POST `/approve`.
6. Bot: `Topic? Duration? Platforms? Count?`
7. Bot: POST `/api/v1/jobs` with `company_id` + `bible_id`.
8. Bot: poll `/api/v1/jobs/{jid}` every 5–30s until terminal.
9. Bot: GET `/jobs/{jid}/outputs` → offer download links.
10. Bot: POST `/jobs/{jid}/publish` per operator intent (draft /
    schedule / publish).

This is the documented hand-off. A real Telegram bot is a separate
follow-up mission; V0 ships only the API surface and the integration
contract.

## What is intentionally not exposed

- **Live HeyGen / ComfyUI / Publer HTTP calls.** All four adapters
  raise `ProviderDisabled` after the gate, regardless of env vars.
  See `docs/provider-matrix.md` and `render_plane/*`.
- **Real authentication.** Bind to localhost. Add auth + reverse-proxy
  before any external exposure.
- **Multi-tenant isolation beyond the data model.** Cross-company
  access is enforced by the API; no row-level security filter is
  applied at the storage layer.