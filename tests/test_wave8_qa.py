"""Wave 8 QA — adversarial tests added by independent QA verification.

These tests exercise the Wave 8 observability surface from a
_black-box_ perspective: each one corresponds to a numbered check
in the QA brief and would catch a real defect if it regressed.

Five new tests, written against HEAD 4c78eb8:

  * test_healthz_does_not_block_on_db_outage
  * test_readyz_failure_response_includes_failure_reasons
  * test_metrics_no_user_workspace_labels
  * test_sentry_skips_pii_fields
  * test_json_log_format_validates_against_schema

The first three are pure observability-contract checks.  The
Sentry test is the **smoking gun** for a defect the dev's own
test suite misses: Wave 8 wires ``init_sentry`` (an SDK no-op
when DSN is unset) but the application never calls
``sentry_sdk.capture_exception`` on unhandled 500s.  An app that
returns a 500 without reporting to Sentry defeats the entire
point of having Sentry in the loop.

The JSON-log test is a structural assertion: every record must
parse as a single JSON object with a stable shape so downstream
log shippers don't choke.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
from unittest import mock

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ────────────────────────────────────────────────────────────────────
# Shared helpers
# ────────────────────────────────────────────────────────────────────


def _build_fresh_app(monkeypatch, *, secret_key="X" * 44, db_key="Y" * 44,
                      db_path=None):
    """Mirror the dev test helper so each test gets a clean slate."""
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", secret_key)
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", db_key)
    if db_path is not None:
        monkeypatch.setenv("VIDEO_FACTORY_DB", str(db_path))
    for mod in [
        "api.observability",
        "api.main",
        "control_plane.secret_vault",
        "control_plane.artifact_hmac",
    ]:
        sys.modules.pop(mod, None)
    from api.main import create_app
    return create_app()


# ────────────────────────────────────────────────────────────────────
# /healthz must not block on DB outage
# ────────────────────────────────────────────────────────────────────


def test_healthz_does_not_block_on_db_outage(tmp_path, monkeypatch):
    """QA brief #2 — ``/healthz`` is a liveness probe and MUST
    return 200 even when the DB is wedged.

    The dev's ``test_healthz_200_always`` only patches ``_conn``;
    a more aggressive test patches it to block forever and proves
    /healthz never gets near it (i.e. there is no synchronous
    SELECT 1 in the route).
    """
    import time
    app = _build_fresh_app(monkeypatch, db_path=str(tmp_path / "h.db"))
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(app)
    # If /healthz ever queried the DB, this would block for 30s.
    def _slow_conn(*a, **kw):
        time.sleep(30)
        raise AssertionError("DB hit by /healthz")
    with mock.patch.object(type(app.state.repo), "_conn", _slow_conn):
        t0 = time.perf_counter()
        r = client.get("/healthz")
        elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    # /healthz must be near-instant.  If we ever waited on _conn(),
    # elapsed would be ~30s.  1s is a generous ceiling.
    assert elapsed < 1.0, f"/healthz took {elapsed:.2f}s, looks like it hit DB"


# ────────────────────────────────────────────────────────────────────
# /readyz must surface the failing check name
# ────────────────────────────────────────────────────────────────────


def test_readyz_failure_response_includes_failure_reasons(tmp_path, monkeypatch):
    """QA brief #3 — /readyz must return 503 with a body that
    names the failing subsystem.

    We test two failure paths: DB down (definitely 503), and the
    vault-key path.  The vault check is a *presence* check
    (env-var non-empty), not a Fernet-validity check; that means
    a too-short key still passes the vault probe.  We assert
    only what the implementation actually guarantees: the body
    *includes* a per-subsystem ``checks`` map so an operator
    can see which one failed, and the DB-down path *does* flip
    to 503 with ``db`` showing the error.
    """
    # 1) DB down — guaranteed 503.
    app = _build_fresh_app(monkeypatch, db_path=str(tmp_path / "r.db"))
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(app)
    with mock.patch.object(
        type(app.state.repo), "_conn",
        side_effect=RuntimeError("database is locked"),
    ):
        r = client.get("/readyz")
    assert r.status_code == 503, r.text
    body = r.json()
    # [t_d967616e adjust 7] /readyz body shape changed per QA D4:
    # {"status": "unready", "checks": {<name>: bool}, "errors": {...}}.
    assert body["status"] == "unready"
    assert "db" in body["checks"], body
    assert body["checks"]["db"] is False, body
    assert body["errors"]["db"], body
    # Also confirm the OTHER checks still report so an operator
    # can see "only db is broken, not vault or master_key".
    assert "vault_key" in body["checks"]
    assert "master_key" in body["checks"]

    # 2) vault_key path: confirm the body shape includes the
    #    per-subsystem map (the brief requires "failure_reasons"
    #    in the body).  Implementation note: vault_key currently
    #    only verifies env-var presence, NOT Fernet validity —
    #    that is a known design tradeoff (a too-short key would
    #    surface at first encrypt() call, not at /readyz), and
    #    is acceptable for the brief.
    fresh = _build_fresh_app(
        monkeypatch, secret_key="", db_key="Y" * 44,
        db_path=str(tmp_path / "r2.db"),
    )
    client2 = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(fresh)
    r2 = client2.get("/readyz")
    body2 = r2.json()
    # Empty env var => vault_key probe should fail.
    # [t_d967616e adjust 7] bool check + reason in ``errors`` (D4).
    assert "vault_key" in body2["checks"]
    assert body2["checks"]["vault_key"] is False, body2
    assert body2["errors"]["vault_key"] == "SecretKeyMissing", body2


# ────────────────────────────────────────────────────────────────────
# /metrics must not have user/workspace labels
# ────────────────────────────────────────────────────────────────────


def test_metrics_no_user_workspace_labels(tmp_path, monkeypatch):
    """QA brief #5 — ``vf_jobs_total`` (and friends) must not
    carry user_id or workspace_id labels, otherwise cardinality
    explodes per user.

    Hammer /healthz a few times to populate the histogram and
    then grep every label set in the /metrics body for forbidden
    substrings.
    """
    app = _build_fresh_app(monkeypatch, db_path=str(tmp_path / "m.db"))
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(app)
    # Make the registry think some jobs ran.
    from api.observability import JOBS_TOTAL, LIVE_SUBMITS_TOTAL, \
        WEBHOOK_RECEIPTS_TOTAL, ERRORS_TOTAL
    JOBS_TOTAL.labels(state="done").inc()
    LIVE_SUBMITS_TOTAL.labels(provider="mock", outcome="ok").inc()
    WEBHOOK_RECEIPTS_TOTAL.labels(source="stripe").inc()
    ERRORS_TOTAL.labels(**{"class": "validation"}).inc()
    # Hit /healthz a few times so the histogram has samples.
    for _ in range(3):
        client.get("/healthz")
    r = client.get("/metrics")
    body = r.text
    forbidden = ["user_id", "workspace_id", "email", "secret",
                 "api_key", "access_token", "ws_", "u_"]
    for tok in forbidden:
        assert tok not in body, (
            f"forbidden label token {tok!r} leaked into /metrics body"
        )


# ────────────────────────────────────────────────────────────────────
# Sentry must be invoked on a real 500
# ────────────────────────────────────────────────────────────────────


def test_sentry_skips_pii_fields(tmp_path, monkeypatch):
    """QA brief #7 + #8 — when a 500 fires the app MUST call
    ``sentry_sdk.capture_exception`` (mock the transport), and
    the captured event MUST NOT include the request's email.

    **This test is the smoking gun for a Wave 8 defect.**  The
    dev shipped ``init_sentry`` (which only registers the SDK
    handler) but the application code never calls
    ``sentry_sdk.capture_exception`` on unhandled exceptions.
    Therefore:

        1. A real 500 → no ``capture_exception`` call (FAIL).
        2. The captured event (if any) leaks the email from the
           request URL → PII defect.

    Both defects are pinned by this test.  When the dev wires a
    generic ``@app.exception_handler(Exception)`` that calls
    ``sentry_sdk.capture_exception(exc)`` AND scrubs PII from the
    request, the test passes.
    """
    monkeypatch.setenv("SENTRY_DSN", "https://public@example/1")
    app = _build_fresh_app(monkeypatch, db_path=str(tmp_path / "s.db"))
    # Rebuild observability so init_sentry sees the env var.
    sys.modules.pop("api.observability", None)
    from api import observability as obs
    obs.SENTRY_ENABLED = False

    # Wire a fake Sentry transport via the SDK.
    import sentry_sdk
    from sentry_sdk.transport import Transport
    captured = []

    # [t_d967616e adjust 1] sentry-sdk 2.x only calls
    # ``capture_envelope`` on a ``Transport`` subclass. The original
    # duck-typed class was wrapped as a deprecated *function*
    # transport and invoked as ``_T(event)``, which raised inside
    # the SDK, so ``captured`` could never fill. Record the event
    # dict carried by each envelope instead.
    class _T(Transport):
        def capture_envelope(self, envelope):
            event = envelope.get_event()
            if event is not None:
                captured.append({"event": event})
        def flush(self, timeout=None, callback=None): pass
        def kill(self): pass

    # [t_d967616e adjust 2] one init through the app's own code path
    # (same options + before_send scrubber prod uses) with the fake
    # transport. The original order (sentry_sdk.init(transport=_T)
    # then obs.init_sentry(dsn)) replaced the fake transport with a
    # real HTTP one before the request was made.
    obs.init_sentry("https://public@example/1", transport=_T)

    # Inject a 500 path AND scrubber-side: when capture_exception
    # is called the event payload should be PII-free.
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(
        app, raise_server_exceptions=False,
    )

    @app.get("/_qa_force_500")
    def _force_500():
        raise RuntimeError("QA_FORCE_500")

    @app.get("/_qa_force_500_with_email")
    def _force_500_email():
        # [t_d967616e adjust 4] the original signature took an
        # unannotated ``request`` param, which FastAPI treats as a
        # required *query* parameter -> 422 before the route body
        # runs, so the PII 500 never happened. Dropped the param.
        # Simulate the kind of PII a route might log on error
        raise RuntimeError(
            "user taras@example.com submitted bad payload in ws_42"
        )

    # 1) Plain 500: did the app call capture_exception?
    r = client.get("/_qa_force_500")
    assert r.status_code == 500, r.text
    sentry_sdk.get_client().flush(timeout=2)
    # Filter out the auto-instrumented httpx / starlette events;
    # the QA brief asks about the APP's call to capture_exception.
    # [t_d967616e adjust 3] ``hint`` never reaches a 2.x transport.
    # An explicit ``sentry_sdk.capture_exception(exc)`` call is
    # recognisable by the SDK's default ``generic`` exception
    # mechanism; starlette/asgi/logging auto-captures carry their
    # own mechanism type.
    def _mechanisms(c):
        ev = c.get("event") or {}
        return [(v.get("mechanism") or {}).get("type")
                for v in (ev.get("exception") or {}).get("values", [])]
    explicit_capture = [
        c for c in captured
        if isinstance(c, dict) and "generic" in _mechanisms(c)
    ]
    # Defect: app should explicitly capture unhandled exceptions.
    assert explicit_capture, (
        "DEFECT: app returned 500 but never called "
        "sentry_sdk.capture_exception(). "
        "Sentry auto-instrumentation is not a substitute; the app "
        "needs a generic @app.exception_handler(Exception) that "
        "forwards to sentry_sdk.capture_exception."
    )

    # 2) 500 with PII in error message: captured event must NOT
    #    contain the email.
    captured.clear()
    r2 = client.get("/_qa_force_500_with_email")
    assert r2.status_code == 500, r2.text
    sentry_sdk.get_client().flush(timeout=2)
    # [t_d967616e adjust 5] the leak check below is vacuous if
    # nothing was captured; require the event to exist.
    assert captured, "no Sentry event captured for the PII 500"
    leaked = False
    for c in captured:
        s = str(c)
        if "taras@example.com" in s or "ws_42" in s:
            leaked = True
            break
    assert not leaked, (
        "DEFECT: Sentry event contains PII (email or workspace id). "
        "The app must scrub request body / query params / exception "
        "message before forwarding to Sentry."
    )
    # [t_d967616e adjust 6] do not leak an active Sentry client
    # into later tests on the same xdist worker.
    sentry_sdk.get_global_scope().set_client(None)


# ────────────────────────────────────────────────────────────────────
# JSON log format must be schema-valid
# ────────────────────────────────────────────────────────────────────


def test_json_log_format_validates_against_schema():
    """QA brief #6 — every line on stdout is a single JSON object
    with a stable shape.

    We exercise the formatter directly with a handful of
    representative records (info, error with extras, error with
    exc_info) and assert each one round-trips through
    ``json.loads`` and matches the documented shape.
    """
    from api.observability import JsonLogFormatter
    fmt = JsonLogFormatter()
    # 1) plain info with extras
    rec1 = logging.LogRecord(
        name="video_factory.test", level=logging.INFO,
        pathname=__file__, lineno=1, msg="hello %s", args=("world",),
        exc_info=None,
    )
    rec1.workspace_id = "ws_42"  # type: ignore[attr-defined]
    out1 = fmt.format(rec1)
    parsed1 = json.loads(out1)
    assert set(parsed1.keys()) >= {"timestamp", "level", "logger", "message"}
    assert parsed1["message"] == "hello world"
    assert parsed1["workspace_id"] == "ws_42"
    # 2) error with exc_info — must serialize to a string
    try:
        raise ValueError("synthetic")
    except ValueError:
        import sys as _sys
        rec2 = logging.LogRecord(
            name="video_factory.test", level=logging.ERROR,
            pathname=__file__, lineno=2, msg="boom",
            args=(), exc_info=_sys.exc_info(),
        )
    out2 = fmt.format(rec2)
    parsed2 = json.loads(out2)
    assert parsed2["level"] == "ERROR"
    assert "exc_info" in parsed2
    assert "ValueError: synthetic" in parsed2["exc_info"]
    # 3) timestamp shape: ISO-8601 UTC with millisecond resolution
    ts = parsed1["timestamp"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", ts), ts
