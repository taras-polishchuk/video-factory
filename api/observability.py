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
import re
import sys
import time
import uuid
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode

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
        # uvicorn attaches an ANSI-coloured duplicate of the message
        # as ``extra={"color_message": ...}``; it is pure noise in JSON.
        "color_message",
    })

    def format(self, record: logging.LogRecord) -> str:
        access = _uvicorn_access_fields(record)
        payload: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.gmtime(record.created),
            ) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": access.pop("_message") if access else record.getMessage(),
        }
        if access:
            payload.update(access)
        # Attach any user-supplied extras (logger.info("x", extra={...}))
        for k, v in record.__dict__.items():
            if k in self._RESERVED_ATTRS or k.startswith("_"):
                continue
            payload[k] = v
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


# Query-string keys whose values must never reach logs or Sentry
# (OIDC ``code``/``state``/``login_hint``/``nonce``, password-reset
# tokens, emails, one-time codes, ...). Substring match on the key,
# case-insensitive: ``pass`` also covers ``password``/``passwd``,
# ``mail`` covers ``email``, ``hint`` covers ``login_hint`` and
# ``id_token_hint`` (QA-N5).
_SENSITIVE_QUERY_KEY_RE = re.compile(
    r"(token|code|state|pass|pwd|secret|key|mail|session|auth|sig|jwt|otp"
    r"|hint|bearer|nonce|ticket|cred|saml)",
    re.IGNORECASE,
)
# A value that carries its own query string (``next=/cb?code=...``,
# a full redirect URL, a ``#access_token=...`` fragment) is redacted
# recursively, at most this deep. Deeper nesting is dropped whole.
_MAX_QS_DEPTH = 3


def redact_query_string(qs: str) -> str:
    """Return ``qs`` with sensitive values replaced by ``[Filtered]``.

    * values of sensitive keys (``_SENSITIVE_QUERY_KEY_RE``) are
      replaced whole;
    * every other key and value is URL-decoded and run through
      ``scrub_text`` (so ``contact=alice%40example.com`` loses the
      email, QA-N3);
    * a value that is itself a URL / query (``next=/cb?code=...``)
      is redacted the same way, recursively (QA-N5).
    """
    if not qs:
        return qs
    return _redact_qs(qs, 0)[0]


def _redact_qs(qs: str, depth: int) -> tuple[str, bool]:
    """``(redacted query string, anything changed?)``."""
    try:
        pairs = parse_qsl(qs, keep_blank_values=True)
    except Exception:  # noqa: BLE001
        return FILTERED, True
    out = []
    changed = False
    for k, v in pairs:
        if _SENSITIVE_QUERY_KEY_RE.search(k):
            out.append((k, FILTERED))
            changed = True
            continue
        nk = scrub_text(k)
        nv = _redact_qs_value(v, depth)
        changed = changed or nk != k or nv != v
        out.append((nk, nv))
    return urlencode(out, safe="[]"), changed


def _redact_qs_value(v: str, depth: int) -> str:
    v = scrub_text(v)
    if "=" not in v:
        return v
    if depth >= _MAX_QS_DEPTH:
        return FILTERED
    cuts = [i for i in (v.find("?"), v.find("#")) if i >= 0]
    cut = min(cuts) if cuts else -1
    head, tail = (v[:cut + 1], v[cut + 1:]) if cut >= 0 else ("", v)
    red, changed = _redact_qs(tail, depth + 1)
    # Untouched nested values keep their original spelling (no
    # re-encoding of an innocent ``a=b`` or base64 ``==`` padding).
    return head + red if changed else v


def _uvicorn_access_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Structured fields for a ``uvicorn.access`` record.

    uvicorn logs access lines as
    ``'%s - "%s %s HTTP/%s" %d'`` with
    ``(client_addr, method, full_path, http_version, status_code)``.
    We lift those into top-level JSON keys and redact sensitive
    query-string values (OIDC ``code``/``state`` etc.) from both
    the ``path`` field and the rendered message. Returns ``{}``
    for any other record.
    """
    if record.name != "uvicorn.access":
        return {}
    args = record.args
    if not isinstance(args, tuple) or len(args) != 5:
        return {}
    client_addr, method, full_path, http_version, status_code = args
    path = str(full_path)
    if "?" in path:
        base, _, qs = path.partition("?")
        path = base + "?" + redact_query_string(qs)
    try:
        status = int(status_code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        status = status_code
    return {
        "_message": f'{client_addr} - "{method} {path} HTTP/{http_version}" {status_code}',
        "client_addr": client_addr,
        "method": method,
        "path": path,
        "http_version": http_version,
        "status_code": status,
    }


class JsonStdoutHandler(logging.StreamHandler):
    """StreamHandler that always writes JSON to the *current* stdout.

    The stream is resolved lazily on every emit (like
    ``logging.lastResort``) so a handler created while pytest's
    capture swapped ``sys.stdout`` never ends up writing to a
    closed buffer. The ``_vf_json`` marker lets
    ``configure_json_logging`` recognise (and replace) handlers it
    or the uvicorn ``--log-config`` file installed.
    """

    _vf_json = True

    def __init__(self, level: int = logging.NOTSET) -> None:
        logging.Handler.__init__(self, level)
        self.setFormatter(JsonLogFormatter())

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, _value) -> None:  # pragma: no cover - ignored on purpose
        pass


# uvicorn installs its own plaintext console handlers on these
# loggers (``uvicorn.config.LOGGING_CONFIG``). We strip them and
# let the records propagate to the root JSON handler.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


def configure_json_logging(level: int = logging.INFO) -> None:
    """Make every log line on stdout a single JSON object.

    * Installs one ``JsonStdoutHandler`` on the root logger
      (replacing any previous copy, so repeated calls never stack).
    * Removes uvicorn's own plaintext console handlers from the
      ``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access`` loggers
      and makes them propagate to root, so startup, access and
      shutdown lines are JSON too (Wave 8 QA defect D2).

    Handlers owned by other parties (pytest's capture handlers,
    an operator's extra file handler) on the root logger are left
    untouched. For lines uvicorn emits *before* the app module is
    imported (``--workers N`` supervisor, reloader), start uvicorn
    with ``--log-config api/uvicorn_log_config.json`` - see
    ``uvicorn_log_config()`` and ``docs/observability.md``.
    """
    root = logging.getLogger()
    # Drop handlers we previously installed so we never end up
    # with N copies of the JSON formatter.
    for h in list(root.handlers):
        if getattr(h, "_vf_json", False):
            root.removeHandler(h)
    root.addHandler(JsonStdoutHandler())
    root.setLevel(level)
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True


def uvicorn_log_config(level: str = "INFO") -> dict[str, Any]:
    """``logging.config.dictConfig`` schema for ``uvicorn --log-config``.

    ``api/uvicorn_log_config.json`` is the checked-in rendering of
    ``uvicorn_log_config()``; a test pins the two together. It
    routes root + every uvicorn logger through ``JsonStdoutHandler``
    before uvicorn imports the app, so even the very first
    ``Started server process`` line is JSON.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {"()": "api.observability.JsonLogFormatter"},
        },
        "handlers": {
            "json_stdout": {
                "class": "api.observability.JsonStdoutHandler",
                "formatter": "json",
            },
        },
        "root": {"handlers": ["json_stdout"], "level": level},
        "loggers": {
            name: {"handlers": [], "level": level, "propagate": True}
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
        },
    }


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
# ``outcome`` on video_factory_live_submits_total.
LIVE_SUBMIT_OUTCOME_LABELS = frozenset({
    "ok", "error", "blocked", "unknown",
})
# ``method`` on video_factory_request_duration_seconds; anything
# else (WebDAV verbs, garbage tokens) is folded into ``OTHER``.
HTTP_METHOD_LABELS = frozenset({
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER",
})
# ``route`` value for requests that matched no FastAPI route.
UNMATCHED_ROUTE = "unmatched"


def route_label(scope: dict) -> str:
    """Bounded ``route`` label: the route template, or ``unmatched``.

    Using the raw URL path for 404s would mint one series per
    scanned path (``/wp-login.php``, ``/.env``, ...).
    """
    route = scope.get("route") if isinstance(scope, dict) else None
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED_ROUTE


def method_label(method: str) -> str:
    """Bounded ``method`` label (see ``HTTP_METHOD_LABELS``)."""
    m = (method or "").upper()
    return m if m in HTTP_METHOD_LABELS else "OTHER"


# ────────────────────────────────────────────────────────────────────
# Sentry hook
# ────────────────────────────────────────────────────────────────────


# Module-level flag so the "did we import sentry?" test can
# distinguish between a previous run that imported the SDK and a
# fresh process where Sentry was not configured. The value is
# only set inside ``init_sentry``; tests that need a clean
# slate can set it back to False.
SENTRY_ENABLED: bool = False


def init_sentry(dsn: Optional[str] = None, *, transport: Any = None) -> bool:
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

    PII policy (Wave 8 QA D1/#8): ``send_default_pii=False``, no
    request bodies (``max_request_body_size="never"``), no frame
    local variables (a credentials PUT would otherwise ship the
    plaintext provider secret), and ``scrub_event`` as
    ``before_send`` for headers, cookies, query strings, user
    fields and any email / id / token pattern left in messages.

    ``transport`` is a test seam: pass a ``sentry_sdk.transport.Transport``
    subclass/instance to capture envelopes in-process.
    """
    global SENTRY_ENABLED
    dsn = (dsn if dsn is not None else os.environ.get("SENTRY_DSN", "")).strip()
    if not dsn:
        SENTRY_ENABLED = False
        return False
    # Lazy import - this is the only path that pulls sentry_sdk.
    import sentry_sdk  # type: ignore  # noqa: F401

    kwargs: dict[str, Any] = dict(
        dsn=dsn,
        # The default integration set is fine; we keep traces_sample_rate
        # at 0 to avoid the perf-cost surprise on first deploy.
        traces_sample_rate=0.0,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        before_send=scrub_event,
    )
    if transport is not None:
        kwargs["transport"] = transport
    sentry_sdk.init(**kwargs)
    SENTRY_ENABLED = True
    return True


def sentry_active() -> bool:
    """True iff a Sentry client with a DSN is currently bound.

    Never imports ``sentry_sdk``: if nothing imported it, Sentry
    cannot be initialised and we return ``False`` for free.
    """
    sdk = sys.modules.get("sentry_sdk")
    if sdk is None:
        return False
    try:
        client = sdk.get_client()
        return bool(client.is_active() and client.dsn)
    except Exception:  # noqa: BLE001
        return False


def capture_exception(exc: BaseException, *, request_id: Optional[str] = None,
                      route: Optional[str] = None,
                      method: Optional[str] = None) -> bool:
    """Forward ``exc`` to Sentry (if initialised) with request tags.

    Returns ``True`` when ``sentry_sdk.capture_exception`` was
    called. The scope gets ``scrub_event`` as an event processor
    too, so the PII policy holds even if the SDK was initialised by
    something other than ``init_sentry`` (defence in depth).
    """
    if not sentry_active():
        return False
    import sentry_sdk  # type: ignore

    with sentry_sdk.new_scope() as scope:
        if request_id:
            scope.set_tag("request_id", request_id)
        if route:
            scope.set_tag("route", route)
        if method:
            scope.set_tag("method", method)
        scope.add_event_processor(scrub_event)
        sentry_sdk.capture_exception(exc)
    return True


# ── request ids ───────────────────────────────────────────────────

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,64}")


def request_id_from(incoming: Optional[str]) -> str:
    """Reuse a well-formed inbound ``X-Request-ID``; otherwise mint one.

    ``fullmatch``: ``re.match`` with ``$`` would accept a trailing
    newline (QA-N6).
    """
    if incoming and _REQUEST_ID_RE.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


# ── PII scrubbing ─────────────────────────────────────────────────

FILTERED = "[Filtered]"

# Header names (lower-case) that are always dropped, plus any
# header whose name contains one of the substrings below.
_SENSITIVE_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip",
    "true-client-ip", "stripe-signature",
})
_SENSITIVE_HEADER_PARTS = ("token", "secret", "signature", "api-key",
                           "apikey", "auth", "csrf", "session")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# ``_EMAIL_RE`` documents the email shape, but it is NOT run with
# ``re.sub``: with no ``@`` in a long ``[A-Za-z0-9._%+-]`` run every
# start offset rescans the run, so the cost is O(n^2) (QA-N1: 32 KB
# took seconds on the event loop). ``_scrub_emails`` finds the same
# matches in one linear pass.
_EMAIL_LOCAL_CHARS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-"
)
_EMAIL_DOMAIN_RUN_RE = re.compile(r"[A-Za-z0-9.-]*")
_ASCII_LETTERS_RE = re.compile(r"[A-Za-z]*")
# Longest string the scrubber looks at. Longer input is cut first so
# the 500 path stays cheap whatever an exception message or query
# carries. The cut drops a trailing partial token so half an email or
# key never survives unscrubbed.
MAX_SCRUB_CHARS = 8192
TRUNCATED = "[truncated]"
_TOKEN_CHARS = _EMAIL_LOCAL_CHARS + "@~/=_"
# Video Factory ids: ``wks_<hex>`` / ``usr_<hex>`` / ``ses_<hex>`` /
# ``cred_<hex>`` (core.repo.new_id), plus the short ``ws_`` / ``u_``
# forms used in docs and tests. At least one digit is required so
# Python identifiers like ``ws_list_jobs`` in stack frames survive.
_ID_RE = re.compile(
    r"\b(?:wks|ws|usr|u|ses|cred|oauth)_(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{1,64}\b"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
# Stripe keys / webhook secrets / Fernet tokens.
_SECRET_RE = re.compile(
    r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]+|\bwhsec_[A-Za-z0-9]+"
    r"|\bgAAAAA[A-Za-z0-9_-]{20,}={0,2}"
)


def _email_domain_end(s: str, start: int) -> int:
    """End index of ``[A-Za-z0-9.-]+\\.[A-Za-z]{2,}`` matched at ``start``.

    Same answer as the greedy regex (the right-most ``.`` that is
    followed by two or more letters, then the whole letter run), in
    one pass over the domain run. ``-1`` when there is no match.
    """
    m = _EMAIL_DOMAIN_RUN_RE.match(s, start)
    run_end = m.end() if m else start
    k = s.rfind(".", start + 1, run_end)
    while k != -1:
        m = _ASCII_LETTERS_RE.match(s, k + 1)
        letters_end = m.end() if m else k + 1
        if letters_end - (k + 1) >= 2:
            return letters_end
        k = s.rfind(".", start + 1, k)
    return -1


def _scrub_emails(s: str, at: str) -> str:
    """Linear-time ``_EMAIL_RE.sub("[email]", s)`` with ``at`` as the
    separator (``"@"``, or ``"%40"`` for percent-encoded addresses).

    Walks the separators left to right. The local part is the run of
    ``_EMAIL_LOCAL_CHARS`` right before the separator, but never
    reaching back past the previous separator or replacement, so every
    character is looked at a bounded number of times.
    """
    out: list[str] = []
    pos = 0      # s[:pos] is already emitted
    floor = 0    # a local part may not start before this index
    i = s.find(at)
    while i != -1:
        after = i + len(at)
        seg = s[max(pos, floor):i]
        local = len(seg) - len(seg.rstrip(_EMAIL_LOCAL_CHARS))
        floor = after
        if local:
            end = _email_domain_end(s, after)
            if end != -1:
                out.append(s[pos:i - local])
                out.append("[email]")
                pos = floor = end
        i = s.find(at, after)
    if not out:
        return s
    out.append(s[pos:])
    return "".join(out)


def scrub_text(s: str) -> str:
    """Redact emails, VF ids, bearer tokens and known secret shapes.

    Linear in ``len(s)`` and capped at ``MAX_SCRUB_CHARS`` (QA-N1):
    it runs synchronously inside the async 500 handler.
    """
    if len(s) > MAX_SCRUB_CHARS:
        s = s[:MAX_SCRUB_CHARS].rstrip(_TOKEN_CHARS) + TRUNCATED
    if "@" in s:
        s = _scrub_emails(s, "@")
    if "%40" in s:
        s = _scrub_emails(s, "%40")
    s = _BEARER_RE.sub(lambda m: f"{m.group(1)} {FILTERED}", s)
    s = _SECRET_RE.sub(FILTERED, s)
    s = _ID_RE.sub("[id]", s)
    return s


def _scrub_value(v: Any, depth: int = 0) -> Any:
    if depth > 32:
        return v
    if isinstance(v, str):
        return scrub_text(v)
    if isinstance(v, dict):
        return {k: _scrub_value(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub_value(x, depth + 1) for x in v]
    if isinstance(v, tuple):
        return tuple(_scrub_value(x, depth + 1) for x in v)
    return v


def _scrub_headers(headers: Any) -> Any:
    if not isinstance(headers, dict):
        return headers
    out = {}
    for k, v in headers.items():
        lk = str(k).lower()
        if lk in _SENSITIVE_HEADERS or any(p in lk for p in _SENSITIVE_HEADER_PARTS):
            out[k] = FILTERED
        else:
            out[k] = v
    return out


def scrub_event(event: dict, hint: Any = None) -> dict:
    """Sentry ``before_send`` / event processor enforcing the PII policy.

    * request: drop body (``data``), cookies and ``env`` (client IP);
      filter auth/cookie/signature/forwarding headers; redact
      sensitive query-string values.
    * user: keep only the opaque ``id``.
    * everything else: recursively redact emails, workspace/user
      ids, bearer tokens, Stripe keys and Fernet tokens from every
      string (exception values, messages, breadcrumbs, source
      context lines).
    """
    if not isinstance(event, dict):
        return event
    req = event.get("request")
    if isinstance(req, dict):
        req.pop("data", None)
        req.pop("cookies", None)
        req.pop("env", None)
        if "headers" in req:
            req["headers"] = _scrub_headers(req["headers"])
        qs = req.get("query_string")
        if isinstance(qs, str) and qs:
            req["query_string"] = redact_query_string(qs)
    user = event.get("user")
    if isinstance(user, dict):
        # Opaque id only; email / username / ip_address never leave.
        event["user"] = {"id": user["id"]} if "id" in user else {}
    for key in list(event.keys()):
        if key == "user":
            continue
        event[key] = _scrub_value(event[key])
    return event
