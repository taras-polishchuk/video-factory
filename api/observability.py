"""Wave 8 observability: JSON logging, Prometheus metrics, Sentry hook.

Three independent concerns live here so they can be exercised in
isolation:

* ``JsonLogFormatter`` - structured JSON formatter for the stdlib
  ``logging`` module. Every record becomes one JSON object on
  stdout (level, logger, message, timestamp, plus any ``extra=``
  fields the call site attached).

* ``metrics`` - module-level ``prometheus_client`` objects. The
  series names and label sets are documented in
  ``docs/observability.md`` and are the contract downstream
  dashboards / alert rules depend on. Label sets are bounded -
  no user-controlled high-cardinality labels (no workspace_id,
  no user_id, no provider secrets).

* ``init_sentry(dsn)`` - opt-in Sentry SDK initialization. When
  the env var is unset the SDK is not imported; the function
  returns ``False`` so callers can fall back to log-only mode
  without ever paying the import cost.

The rest of the API module just imports these symbols and uses
them; no global state is mutated at import time other than the
metric definitions (which are required by ``prometheus_client``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)


# ────────────────────────────────────────────────────────────────────
# JSON logging
# ────────────────────────────────────────────────────────────────────


class JsonLogFormatter(logging.Formatter):
    """Format every LogRecord as a single JSON object on one line.

    Reserved keys (``timestamp``, ``level``, ``logger``, ``message``)
    are always present. Anything the call site attached via
    ``logger.info("...", extra={"key": "value"})`` is merged in at
    the top level. The stdlib's own ``exc_info`` is serialized as
    a string under ``exc_info`` so the resulting line is still
    valid JSON.
    """

    # Stdlib LogRecord attributes that are not user data and that
    # we should NOT bleed into the JSON output (they duplicate
    # ``message``/``asctime`` or carry no useful info).
    _RESERVED_ATTRS = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "asctime",
        "message", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.gmtime(record.created),
            ) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Attach any user-supplied extras (logger.info("x", extra={...}))
        for k, v in record.__dict__.items():
            if k in self._RESERVED_ATTRS or k.startswith("_"):
                continue
            payload[k] = v
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_json_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger.

    Safe to call multiple times - the handler is replaced in
    place rather than stacked. We keep ``propagate=True`` so
    module loggers (api, core, control_plane) flow through the
    root handler; tests that inspect a specific logger still
    get the same JSON output.
    """
    root = logging.getLogger()
    # Drop handlers we previously installed so we never end up
    # with N copies of the JSON formatter.
    for h in list(root.handlers):
        if getattr(h, "_vf_json", False):
            root.removeHandler(h)
    h = logging.StreamHandler()
    h.setFormatter(JsonLogFormatter())
    h._vf_json = True  # type: ignore[attr-defined]
    root.addHandler(h)
    root.setLevel(level)


# ────────────────────────────────────────────────────────────────────
# Prometheus metrics
# ────────────────────────────────────────────────────────────────────


# A dedicated registry so unit tests can reset between runs and
# we never collide with the default global registry. The
# ``/metrics`` endpoint serves ``generate_latest(REGISTRY)``.
REGISTRY = CollectorRegistry()

# Job counts broken down by lifecycle state. ``state`` is bounded
# to the JobRunner state machine ({queued, running, done, failed,
# canceled}); the producer is responsible for coercing unknown
# values to "unknown" so a buggy code path cannot blow up the
# label cardinality.
JOBS_TOTAL = Counter(
    "video_factory_jobs_total",
    "Total jobs that entered each terminal-or-running state.",
    labelnames=("state",),
    registry=REGISTRY,
)

# Live provider submit attempts. ``provider`` is bounded to the
# built-in adapter set ({heygen_direct, comfy_cloud, mock}); the
# runner emits a counter on every live submit attempt so failed
# attempts are still observable.
LIVE_SUBMITS_TOTAL = Counter(
    "video_factory_live_submits_total",
    "Live-provider submit attempts by provider and outcome.",
    labelnames=("provider", "outcome"),
    registry=REGISTRY,
)

# Webhook receipt counts. ``source`` is the canonical provider
# slug ({stripe, heygen, internal}). We deliberately do NOT
# include a status label - per the brief, the bounded
# "received/ignored/handled" enum lives in the audit row, and
# the metric is a firehose.
WEBHOOK_RECEIPTS_TOTAL = Counter(
    "video_factory_webhook_receipts_total",
    "Webhook receipts by source.",
    labelnames=("source",),
    registry=REGISTRY,
)

# Error counts. ``class`` is the high-level bucket ({auth,
# validation, quota, vault, repo, runtime}); the runner / API
# coerces to this enum before incrementing.
ERRORS_TOTAL = Counter(
    "video_factory_errors_total",
    "Errors raised by the API, bucketed by class.",
    labelnames=("class",),
    registry=REGISTRY,
)

# Request duration histogram. The bucket boundaries cover sub-ms
# cache hits through slow 2s+ SQLCipher ops; default buckets are
# too coarse for the p95 we care about. ``route`` is the FastAPI
# route template, not the raw path - so /api/v1/workspaces/{wid}
# is one label value, not one per workspace.
REQUEST_DURATION = Histogram(
    "video_factory_request_duration_seconds",
    "Request duration in seconds by route.",
    labelnames=("route", "method", "status"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
             1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)


def metrics_response() -> bytes:
    """Render the current registry in Prometheus text format.

    Returned as ``bytes`` so FastAPI can hand it to a
    ``Response(content=..., media_type=...)`` without re-encoding.
    """
    return generate_latest(REGISTRY)


# Bounded label-value enums. Producers MUST use these (or
# substitute "unknown") so the metric cardinality stays bounded
# regardless of operator-controlled input.
JOB_STATE_LABELS = frozenset({
    "queued", "running", "done", "failed", "canceled", "unknown",
})
LIVE_PROVIDER_LABELS = frozenset({
    "heygen_direct", "comfy_cloud", "mock", "unknown",
})
WEBHOOK_SOURCE_LABELS = frozenset({
    "stripe", "heygen", "internal", "unknown",
})
ERROR_CLASS_LABELS = frozenset({
    "auth", "validation", "quota", "vault", "repo", "runtime",
    "unknown",
})


# ────────────────────────────────────────────────────────────────────
# Sentry hook
# ────────────────────────────────────────────────────────────────────


# Module-level flag so the "did we import sentry?" test can
# distinguish between a previous run that imported the SDK and a
# fresh process where Sentry was not configured. The value is
# only set inside ``init_sentry``; tests that need a clean
# slate can set it back to False.
SENTRY_ENABLED: bool = False


def init_sentry(dsn: Optional[str] = None) -> bool:
    """Initialize the Sentry SDK if a DSN is present.

    * When ``dsn`` is falsy (env var unset / empty), the SDK is
      NOT imported and the function returns ``False``. The
      caller should treat the app as log-only.
    * When ``dsn`` is set, the SDK is imported lazily, the
      handler is registered, and ``SENTRY_ENABLED`` is flipped
      to ``True`` so tests can assert the integration fired.

    Importing the SDK lazily is the whole point - the brief
    requires that an unset ``SENTRY_DSN`` env var results in
    zero sentry-sdk import cost.
    """
    global SENTRY_ENABLED
    dsn = (dsn if dsn is not None else os.environ.get("SENTRY_DSN", "")).strip()
    if not dsn:
        SENTRY_ENABLED = False
        return False
    # Lazy import - this is the only path that pulls sentry_sdk.
    import sentry_sdk  # type: ignore  # noqa: F401

    sentry_sdk.init(
        dsn=dsn,
        # The default integration set is fine; we keep traces_sample_rate
        # at 0 to avoid the perf-cost surprise on first deploy.
        traces_sample_rate=0.0,
    )
    SENTRY_ENABLED = True
    return True
