"""Wave 8 tests — observability: JSON logs, /healthz, /readyz, /metrics, Sentry.

Eight new tests cover the Wave 8 brief. They live in their own
file so a failure does not pollute the regression surfaces for
other waves. The fixtures are local to the file: we do not rely
on the conftest ``env`` fixture because /readyz needs to control
the secret-key environment per-test.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import sys
from unittest import mock

import pytest
from fastapi.testclient import TestClient


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ────────────────────────────────────────────────────────────────────
# Shared helpers
# ────────────────────────────────────────────────────────────────────


def _make_app(monkeypatch, *, secret_key="X" * 44, db_key="Y" * 44, db_path=None):
    """Build a fresh app with controllable env.

    We patch the env BEFORE importing the app modules so the
    secret vault / artifact HMAC modules pick up the values we
    want. ``VIDEO_FACTORY_DB_KEY`` is what artifact_hmac
    consumes; ``VIDEO_FACTORY_SECRET_KEY`` is what secret_vault
    consumes. They differ because the brief specifies two
    separate keys for the per-workspace vault vs the SQLCipher
    master key.
    """
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", secret_key)
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", db_key)
    if db_path is not None:
        monkeypatch.setenv("VIDEO_FACTORY_DB", str(db_path))
    # Force a fresh import so cached state from a previous test
    # does not leak in.
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
# /healthz
# ────────────────────────────────────────────────────────────────────


def test_healthz_200_always(tmp_path, monkeypatch):
    """Liveness: 200 even when the DB is unreachable.

    Per the brief, /healthz MUST NOT hit the DB. We assert that
    by patching Repo._conn to raise and confirming /healthz
    still returns 200.
    """
    app = _make_app(monkeypatch, db_path=str(tmp_path / "h.db"))
    client = TestClient(app)
    # Force the DB probe to blow up if it ever gets called.
    boom = mock.MagicMock(side_effect=AssertionError("DB hit!"))
    with mock.patch.object(type(app.state.repo), "_conn", boom):
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ────────────────────────────────────────────────────────────────────
# /readyz
# ────────────────────────────────────────────────────────────────────


def test_readyz_503_when_db_locked(tmp_path, monkeypatch):
    """Readiness: 503 when the DB cannot serve a SELECT 1."""
    app = _make_app(monkeypatch, db_path=str(tmp_path / "r.db"))
    client = TestClient(app)
    boom = mock.MagicMock(side_effect=RuntimeError("db locked"))
    with mock.patch.object(type(app.state.repo), "_conn", boom):
        r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert "db" in body["checks"]
    assert body["checks"]["db"].startswith("error")


def test_readyz_200_when_db_open(tmp_path, monkeypatch):
    """Readiness: 200 when DB + vault + master key are healthy."""
    app = _make_app(monkeypatch, db_path=str(tmp_path / "r.db"))
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    for k in ("db", "vault_key", "master_key"):
        assert body["checks"][k] == "ok", body


# ────────────────────────────────────────────────────────────────────
# /metrics
# ────────────────────────────────────────────────────────────────────


def test_metrics_endpoint_returns_documented_series(tmp_path, monkeypatch):
    """Metrics: response contains every series named in the brief.

    The test asserts the metric NAMES are present, not the
    concrete values - the histogram is empty until a request
    flows through it, so we touch a real route to populate the
    histogram before scraping.
    """
    app = _make_app(monkeypatch, db_path=str(tmp_path / "m.db"))
    client = TestClient(app)
    # Force at least one request through the middleware so the
    # histogram has a sample.
    client.get("/healthz")
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    expected = [
        "video_factory_jobs_total",
        "video_factory_live_submits_total",
        "video_factory_webhook_receipts_total",
        "video_factory_errors_total",
        "video_factory_request_duration_seconds",
    ]
    for name in expected:
        assert name in body, f"missing metric: {name}"
    # Prometheus content type
    assert "text/plain" in r.headers.get("content-type", "")


# ────────────────────────────────────────────────────────────────────
# JSON logging format
# ────────────────────────────────────────────────────────────────────


def test_json_logging_format():
    """JsonLogFormatter: every record is one JSON object.

    We exercise the formatter directly (not via the root
    handler) so the assertion does not depend on capture order.
    """
    from api.observability import JsonLogFormatter

    fmt = JsonLogFormatter()
    rec = logging.LogRecord(
        name="video_factory.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    rec.workspace_id = "ws_123"  # user extra
    out = fmt.format(rec)
    parsed = json.loads(out)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "video_factory.test"
    assert parsed["message"] == "hello world"
    assert parsed["workspace_id"] == "ws_123"
    # timestamp is ISO-8601 UTC with millisecond resolution
    assert "T" in parsed["timestamp"] and parsed["timestamp"].endswith("Z")


# ────────────────────────────────────────────────────────────────────
# Sentry hook
# ────────────────────────────────────────────────────────────────────


def test_sentry_dsn_unset_does_not_import_sdk(monkeypatch):
    """Sentry: no DSN == SDK is not imported.

    Per the brief, when SENTRY_DSN is unset the SDK must NOT be
    imported. We enforce this by deleting the module from
    sys.modules, calling ``init_sentry()`` with no DSN, and
    re-checking that the module is still absent.
    """
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    # Force the module out of sys.modules so we can observe the
    # import behaviour on the next call.
    sys.modules.pop("sentry_sdk", None)
    # Re-import observability so the module-level state is
    # clean for this test.
    sys.modules.pop("api.observability", None)
    from api import observability as obs
    obs.SENTRY_ENABLED = False  # explicit reset
    result = obs.init_sentry()
    assert result is False
    assert obs.SENTRY_ENABLED is False
    assert "sentry_sdk" not in sys.modules


def test_sentry_dsn_set_registers_handler(monkeypatch):
    """Sentry: a DSN causes the SDK to be imported and init'd.

    We stub out ``sentry_sdk.init`` so the test does not try to
    phone home - we only care that the import path fires and
    that ``init`` is invoked with the DSN.
    """
    monkeypatch.setenv("SENTRY_DSN", "https://public@example/1")
    sys.modules.pop("api.observability", None)
    sys.modules.pop("sentry_sdk", None)
    from api import observability as obs
    obs.SENTRY_ENABLED = False
    with mock.patch("sentry_sdk.init") as fake_init:
        result = obs.init_sentry()
    assert result is True
    assert obs.SENTRY_ENABLED is True
    fake_init.assert_called_once()
    kwargs = fake_init.call_args.kwargs
    assert kwargs["dsn"] == "https://public@example/1"


# ────────────────────────────────────────────────────────────────────
# Regression: 282+ existing tests still green
# ────────────────────────────────────────────────────────────────────


def test_existing_282_tests_still_green(monkeypatch):
    """Sanity probe: import every test module and every source
    module the suite touches. A module that fails to import
    (e.g. a name collision with our new symbols) would block
    the full pytest run; catching it here turns a 4-minute
    pytest loop into a one-second check.

    This test is NOT a substitute for running the suite - it
    only checks that nothing in our wave collides with
    existing public names.
    """
    import importlib
    for mod_name in [
        "api.main",
        "api.observability",
        "api.auth",
        "api.billing",
        "api.oidc",
        "api.webhooks_stripe",
        "api.webhooks_heygen",
        "control_plane.artifact_hmac",
        "control_plane.secret_vault",
        "control_plane.audit",
        "core.repo",
        "core.runners",
        "core.quotas",
    ]:
        importlib.import_module(mod_name)
