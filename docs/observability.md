# Observability

Video Factory exposes four operational surfaces:

| Surface | Purpose |
|---|---|
| `GET /healthz` | Liveness probe. Never touches the DB. |
| `GET /readyz` | Readiness probe. DB, vault key, master key. |
| `GET /metrics` | Prometheus scrape (text format 0.0.4). |
| stdout | JSON logs, one object per line. |
| Sentry | Unhandled exceptions, PII-scrubbed. Opt-in via `SENTRY_DSN`. |

Code: `api/observability.py` (formatter, registry, Sentry hooks) and
`api/main.py` (probes, middleware, global 500 handler).
Dashboard: `infra/grafana/video-factory-dashboard.json`.

Tests pin this document to the code: `tests/test_wave8_followup.py`
parses the metrics table below and fails if a series, type, label or
allowed value drifts from `api/observability.py`, or if the Grafana
dashboard references a series that does not exist.

---

## /healthz vs /readyz

| | `/healthz` | `/readyz` |
|---|---|---|
| Question | Is the process alive? | Can it serve real traffic now? |
| Touches DB | No | Yes, one `SELECT 1` |
| Success | `200 {"ok": true}` | `200`, `status: "ready"` |
| Failure | Process dead, no answer | `503`, `status: "unready"` |
| On failure, orchestrator should | Restart the container | Stop routing traffic. Do NOT restart. |

`/health` (legacy, pre-Wave 8) is kept for the Docker `HEALTHCHECK`
and the deploy smoke check. It does not touch the DB either.

### /readyz body

```json
{
  "status": "unready",
  "checks": {"db": false, "vault_key": true, "master_key": true},
  "errors": {"db": "OperationalError"},
  "warnings": {}
}
```

* `status`: `ready` or `unready`. HTTP `200` or `503`.
* `checks`: one bool per check. All must be `true` for `ready`.
* `errors`: only failing checks. Value is a reason code: an exception
  class name or a fixed token. Never an exception message, so no
  path or key material leaks through an unauthenticated probe.
* `warnings`: non-fatal findings. Currently only
  `master_key: dev_key_in_use` outside production.

| Check | Passes when | Reason codes |
|---|---|---|
| `db` | `SELECT 1` returns `1` | exception class (e.g. `OperationalError`), `unhealthy` |
| `vault_key` | `VIDEO_FACTORY_SECRET_KEY` builds a Fernet (32 url-safe base64 bytes) | `SecretKeyMissing`, `SecretKeyInvalid` |
| `master_key` | artifact HMAC key resolves. With `VIDEO_FACTORY_ENV=production` the static dev fallback key is refused. | `dev_key_in_use`, exception class |

The production flag is `VIDEO_FACTORY_ENV=production`, the same flag
that turns on `Secure` session cookies (`auth.session.is_production`).

---

## Metrics

Served from a dedicated registry (`api.observability.REGISTRY`), not
the global default one. Every label is bounded: no user id, workspace
id, email, raw path or provider secret ever becomes a label value.
Producers coerce anything outside the allowed set to `unknown`.

| Series | Type | Labels | Allowed values | Producer |
|---|---|---|---|---|
| `video_factory_jobs_total` | counter | `state` | state: `queued`, `running`, `done`, `failed`, `canceled`, `unknown` | Not wired yet (follow-up). Exposes HELP/TYPE only. |
| `video_factory_live_submits_total` | counter | `provider`, `outcome` | provider: `heygen_direct`, `comfy_cloud`, `mock`, `unknown`; outcome: `ok`, `error`, `blocked`, `unknown` | Not wired yet (follow-up). Exposes HELP/TYPE only. |
| `video_factory_webhook_receipts_total` | counter | `source` | source: `stripe`, `heygen`, `internal`, `unknown` | Not wired yet (follow-up). Exposes HELP/TYPE only. |
| `video_factory_errors_total` | counter | `class` | class: `auth`, `validation`, `quota`, `vault`, `repo`, `runtime`, `unknown` | Global 500 handler increments `class="runtime"`. Other classes reserved. |
| `video_factory_request_duration_seconds` | histogram | `route`, `method`, `status` | method: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`, `OTHER` | HTTP middleware, every request except `/metrics`. |

Free-form but bounded labels:

* `route`: FastAPI route template, e.g. `/api/v1/workspaces/{wid}/jobs`.
  Requests that match no route (404 scans) get `unmatched`, never the
  raw path.
* `status`: HTTP status code as a string (`200`, `404`, `500`, ...).
  An exception that escapes the app is recorded as `500`.

Histogram buckets (seconds): `0.005`, `0.01`, `0.025`, `0.05`, `0.1`,
`0.25`, `0.5`, `1`, `2.5`, `5`, `10`. The histogram also exposes the
standard `_bucket`, `_count`, `_sum` and `_created` series.

### Useful queries

```promql
# Request rate by route
sum by (route) (rate(video_factory_request_duration_seconds_count[5m]))

# p95 latency by route
histogram_quantile(0.95, sum by (le, route) (rate(video_factory_request_duration_seconds_bucket[5m])))

# 5xx ratio
sum(rate(video_factory_request_duration_seconds_count{status=~"5.."}[5m]))
  / sum(rate(video_factory_request_duration_seconds_count[5m]))

# Unhandled exceptions per second
sum by (class) (rate(video_factory_errors_total[5m]))
```

### Alert suggestions

Starting points. Tune thresholds after a week of real traffic.

| Alert | Expression | For | Severity |
|---|---|---|---|
| API down | `up{job="video-factory-api"} == 0` | 2m | page |
| Not ready | blackbox probe of `/readyz` returns non-200 | 5m | page |
| High 5xx ratio | 5xx ratio query above `> 0.05` | 10m | page |
| Unhandled exceptions | `sum(rate(video_factory_errors_total{class="runtime"}[5m])) > 0.1` | 10m | ticket |
| Slow p95 | `histogram_quantile(0.95, sum by (le) (rate(video_factory_request_duration_seconds_bucket[5m]))) > 0.5` | 15m | ticket |
| Job failures (once wired) | `sum(increase(video_factory_jobs_total{state="failed"}[1h])) > 5` | 0m | ticket |
| Webhooks silent (once wired) | `sum(increase(video_factory_webhook_receipts_total{source="stripe"}[24h])) == 0` | 0m | ticket |

### Exposure

`/metrics` carries no PII, but it does reveal traffic shape. The
current `Caddyfile` forwards `/metrics*` publicly. Restrict it to the
Prometheus scraper (IP allowlist or basic auth at Caddy) before going
live. `/readyz` is not forwarded by Caddy; probe it from inside the
host or container network.

---

## Logs

Every line on stdout is one JSON object (`api.observability.JsonLogFormatter`).
This includes uvicorn's own startup, access and shutdown lines.

Two layers make that true:

1. `configure_json_logging()` runs inside `create_app()`. It installs
   one `JsonStdoutHandler` on the root logger, strips uvicorn's
   plaintext console handlers from `uvicorn`, `uvicorn.error`,
   `uvicorn.access` and lets them propagate to root.
2. `api/uvicorn_log_config.json` is passed as
   `uvicorn ... --log-config api/uvicorn_log_config.json` by the
   `Dockerfile`, `docker-compose.production.yml` and `docker-compose.yml`
   (systemd runs the production compose file). It applies the same
   setup *before* the app is imported, so lines from the `--workers N`
   supervisor process are JSON too. The file is the rendering of
   `api.observability.uvicorn_log_config()`; a test keeps them equal.

### Fields

| Field | When | Meaning |
|---|---|---|
| `timestamp` | always | ISO-8601 UTC, millisecond precision, `Z` suffix |
| `level` | always | `DEBUG` .. `CRITICAL` |
| `logger` | always | logger name, e.g. `uvicorn.access`, `video_factory.errors` |
| `message` | always | rendered message |
| `exc_info` | record has an exception | full traceback as one string |
| `stack_info` | `stack_info=True` | stack as one string |
| `client_addr`, `method`, `path`, `http_version`, `status_code` | `uvicorn.access` | request line, structured. Sensitive query values (`code`, `state`, `token`, `email`, ...) are replaced by `[Filtered]` in `path` and `message`. |
| `request_id`, `route`, `method`, `error_class` | `video_factory.errors` | unhandled exception. `request_id` matches the 500 body, the `X-Request-ID` response header and the Sentry tag. |
| any `extra=` key | call site passed `extra={...}` | merged at top level |

Application logs are not PII-scrubbed. Do not log emails, secrets or
request bodies. The unhandled-exception line includes the traceback,
which includes the exception message.

---

## Errors and Sentry

### Unhandled exceptions

`api/main.py` registers `@app.exception_handler(Exception)`. For any
exception no specific handler claims, it:

1. increments `video_factory_errors_total{class="runtime"}`,
2. calls `sentry_sdk.capture_exception` when Sentry is initialised,
   tagged with `request_id`, `route`, `method`,
3. logs one `video_factory.errors` JSON line with the same `request_id`,
4. returns `500 {"error": "internal server error", "request_id": "<id>"}`
   plus an `X-Request-ID` header. Never the exception text, never a
   stack trace.

A well-formed inbound `X-Request-ID` (8 to 64 chars of `A-Za-z0-9._-`)
is reused, so an id set by the proxy flows through. Otherwise a new
uuid4 hex is minted.

### Setup

| Env var | Effect |
|---|---|
| `SENTRY_DSN` | Enables Sentry. Unset or empty: `sentry_sdk` is never imported. |
| `SENTRY_ENVIRONMENT` | Read natively by the SDK. Suggest `production` / `staging`. |
| `SENTRY_RELEASE` | Read natively by the SDK. Suggest the image tag or git SHA. |

`init_sentry()` options: `traces_sample_rate=0.0` (errors only),
`send_default_pii=False`, `max_request_body_size="never"`,
`include_local_variables=False`, `before_send=scrub_event`.

### PII policy

Enforced by `api.observability.scrub_event`, installed both as
`before_send` and as a scope event processor on every app capture:

* No request bodies, on any route (covers `/auth/*`, `/api/v1/auth/*`,
  credential `PUT`s, webhooks).
* No cookies. No client IP (`request.env`, `X-Forwarded-For`,
  `X-Real-IP`, `CF-Connecting-IP`).
* Headers `Authorization`, `Cookie`, `Set-Cookie`, `Stripe-Signature`
  and any header whose name contains `token`, `secret`, `signature`,
  `api-key`, `auth`, `csrf` or `session` become `[Filtered]`.
* Query values for keys like `code`, `state`, `token`, `password`,
  `email`, `key` become `[Filtered]`.
* `user` keeps only `id`.
* Every remaining string (exception value, message, breadcrumbs,
  source context lines) is scanned: emails become `[email]`, Video
  Factory ids (`wks_…`, `usr_…`, `ses_…`, `cred_…`, `ws_…`, `u_…`)
  become `[id]`, `Bearer …` / `Basic …` credentials, Stripe keys
  (`sk_live_…`, `whsec_…`) and Fernet tokens become `[Filtered]`.
* No frame local variables are sent (a credentials `PUT` would
  otherwise ship the plaintext provider secret).
