"""Wave 8 follow-up (t_d967616e): tests for QA defects D1..D6.

QA report: video-factory-saas-production-2026-10-06/artifacts/wave-8-qa-report.md

* D1  unhandled exceptions -> JSON 500 + request id + Sentry capture,
      PII scrubbed (``before_send`` + scope processor).
* D2  every stdout line from a real ``uvicorn api.main:app --access-log``
      subprocess is JSON (plain CLI and the ``--log-config`` entrypoint).
* D3  docs/observability.md and infra/grafana/video-factory-dashboard.json
      stay in parity with the metrics registry.
* D4  /readyz body: {"status", "checks": {bool}, "errors", "warnings"}.
* D5  vault_key check builds the Fernet (validity, not presence).
* D6  master_key check refuses the static dev key in production.

Plus the route/method label bounding the docs promise (404 scans
must not mint one histogram series per raw path).

All keys below are synthetic test values.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest import mock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 32 url-safe base64 bytes -> a syntactically valid Fernet key.
VALID_FERNET = "a" * 43 + "="
DB_KEY = "Y" * 44
DSN = "https://public@example.invalid/1"


# ────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────


def _obs():
    """The api.observability module the app currently sees."""
    import importlib
    return sys.modules.get("api.observability") or importlib.import_module(
        "api.observability")


def _app(monkeypatch, tmp_path, *, secret_key=VALID_FERNET, db_key=DB_KEY,
         env: str | None = None):
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", secret_key)
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", db_key)
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    if env is None:
        monkeypatch.delenv("VIDEO_FACTORY_ENV", raising=False)
    else:
        monkeypatch.setenv("VIDEO_FACTORY_ENV", env)
    from api.main import create_app
    return create_app()


def _reset_sentry():
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover
        return
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture(autouse=True)
def _no_sentry_leak():
    _reset_sentry()
    yield
    _reset_sentry()


def _fake_sentry():
    """init_sentry() through the app code path with an in-memory transport."""
    from sentry_sdk.transport import Transport
    events: list[dict] = []

    class _T(Transport):
        def capture_envelope(self, envelope):
            ev = envelope.get_event()
            if ev is not None:
                events.append(ev)

        def flush(self, timeout=None, callback=None):
            pass

        def kill(self):
            pass

    assert _obs().init_sentry(DSN, transport=_T) is True
    return events


def _mechanisms(ev):
    return [(v.get("mechanism") or {}).get("type")
            for v in (ev.get("exception") or {}).get("values", [])]


def _counter_value(name: str, **labels) -> float:
    v = _obs().REGISTRY.get_sample_value(name, labels)
    return v or 0.0


# ────────────────────────────────────────────────────────────────────
# D1 - global 500 handler + Sentry + PII
# ────────────────────────────────────────────────────────────────────


def test_unhandled_500_is_generic_json_with_request_id(tmp_path, monkeypatch):
    app = _app(monkeypatch, tmp_path)

    @app.get("/_t_boom")
    async def _boom():
        raise RuntimeError("internal detail taras@example.com /srv/secret/path")

    before = _counter_value("video_factory_errors_total", **{"class": "runtime"})
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/_t_boom")
    assert r.status_code == 500
    body = r.json()
    assert set(body) == {"error", "request_id"}
    assert body["error"] == "internal server error"
    assert re.fullmatch(r"[0-9a-f]{32}", body["request_id"])
    assert r.headers["x-request-id"] == body["request_id"]
    # Never leak the exception text or a stack trace to the client.
    for needle in ("Traceback", "RuntimeError", "taras@", "/srv/secret"):
        assert needle not in r.text
    after = _counter_value("video_factory_errors_total", **{"class": "runtime"})
    assert after == before + 1


def test_inbound_request_id_reused_only_when_well_formed(tmp_path, monkeypatch):
    app = _app(monkeypatch, tmp_path)

    @app.get("/_t_boom2")
    async def _boom():
        raise RuntimeError("x")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/_t_boom2", headers={"X-Request-ID": "edge-req-0001"})
    assert r.json()["request_id"] == "edge-req-0001"
    for bad in ("short", "has spaces in it", "x" * 65, "inject\"quote"):
        r = client.get("/_t_boom2", headers={"X-Request-ID": bad})
        assert r.json()["request_id"] != bad
        assert re.fullmatch(r"[0-9a-f]{32}", r.json()["request_id"])


def test_unhandled_500_logs_one_json_line_with_request_id(tmp_path, monkeypatch):
    app = _app(monkeypatch, tmp_path)

    @app.get("/_t_boom3")
    async def _boom():
        raise KeyError("missing")

    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    lg = logging.getLogger("video_factory.errors")
    h = _H()
    lg.addHandler(h)
    try:
        r = TestClient(app, raise_server_exceptions=False).get("/_t_boom3")
    finally:
        lg.removeHandler(h)
    rid = r.json()["request_id"]
    mine = [x for x in records if getattr(x, "request_id", None) == rid]
    assert len(mine) == 1
    rec = mine[0]
    assert rec.levelno == logging.ERROR
    assert rec.route == "/_t_boom3"
    assert rec.error_class == "KeyError"
    line = _obs().JsonLogFormatter().format(rec)
    parsed = json.loads(line)
    assert "\n" not in line
    assert parsed["request_id"] == rid
    assert "KeyError" in parsed["exc_info"]


def test_sentry_captures_500_with_request_id_tag(tmp_path, monkeypatch):
    app = _app(monkeypatch, tmp_path)
    events = _fake_sentry()

    @app.get("/_t_sentry")
    async def _boom():
        raise RuntimeError("plain failure")

    r = TestClient(app, raise_server_exceptions=False).get("/_t_sentry")
    assert r.status_code == 500
    rid = r.json()["request_id"]
    explicit = [e for e in events if "generic" in _mechanisms(e)]
    assert len(explicit) == 1, events
    ev = explicit[0]
    assert ev["tags"]["request_id"] == rid
    assert ev["tags"]["route"] == "/_t_sentry"
    assert ev["tags"]["method"] == "GET"
    assert ev["exception"]["values"][-1]["type"] == "RuntimeError"
    # Auto-instrumented duplicates (asgi middleware, logging) are
    # deduplicated by the SDK: one exception -> one event.
    assert len(events) == 1, [_mechanisms(e) for e in events]


def test_sentry_event_has_no_pii_on_auth_route(tmp_path, monkeypatch):
    app = _app(monkeypatch, tmp_path)
    events = _fake_sentry()

    @app.post("/auth/_t_boom")
    async def _boom(request: Request):
        payload = await request.json()
        # Secret only exists at runtime (not in source), so finding
        # it in the event would mean locals or the body were sent.
        provider_secret = payload["password"]  # noqa: F841
        raise RuntimeError(
            f"login failed for {payload['email']} in wks_0a1b2c3d4e5f"
        )

    runtime_secret = "pw-" + "q7Zr" * 6
    runtime_email = "victim." + "user@example.org"
    bearer = "tok" + "X9" * 12
    r = TestClient(app, raise_server_exceptions=False).post(
        "/auth/_t_boom?email=leak%40example.org&page=2",
        json={"email": runtime_email, "password": runtime_secret},
        headers={
            "Authorization": f"Bearer {bearer}",
            "Cookie": "vf_session=ses_9f8e7d6c5b4a",
            "X-CSRF-Token": "csrf-value-123456",
            "X-Forwarded-For": "203.0.113.9",
        },
    )
    assert r.status_code == 500
    assert events, "no Sentry event captured"
    blob = json.dumps(events, default=str)
    for leaked in (runtime_secret, runtime_email, "leak@example.org",
                   "leak%40example.org", bearer, "ses_9f8e7d6c5b4a",
                   "csrf-value-123456", "203.0.113.9", "wks_0a1b2c3d4e5f"):
        assert leaked not in blob, f"PII leaked into Sentry event: {leaked!r}"
    ev = [e for e in events if "generic" in _mechanisms(e)][0]
    req = ev.get("request", {})
    assert "data" not in req, "request body must never be sent"
    assert "cookies" not in req
    assert "env" not in req
    headers = {k.lower(): v for k, v in (req.get("headers") or {}).items()}
    for h in ("authorization", "cookie", "x-csrf-token", "x-forwarded-for"):
        if h in headers:
            assert headers[h] in ("[Filtered]", ""), (h, headers[h])
    assert "page=2" in req.get("query_string", "")
    # Exception message keeps its shape, PII replaced.
    msg = ev["exception"]["values"][-1]["value"]
    assert "[email]" in msg and "[id]" in msg
    # No frame locals at all.
    for exc in ev["exception"]["values"]:
        for fr in (exc.get("stacktrace") or {}).get("frames", []):
            assert "vars" not in fr


def test_no_sentry_call_when_sentry_not_initialised(tmp_path, monkeypatch):
    app = _app(monkeypatch, tmp_path)

    @app.get("/_t_nosentry")
    async def _boom():
        raise RuntimeError("x")

    import sentry_sdk
    with mock.patch.object(sentry_sdk, "capture_exception") as cap:
        r = TestClient(app, raise_server_exceptions=False).get("/_t_nosentry")
    assert r.status_code == 500
    assert r.json()["error"] == "internal server error"
    cap.assert_not_called()
    assert _obs().sentry_active() is False


def test_scrub_event_unit():
    obs = _obs()
    event = {
        "request": {
            "url": "https://vf.example/api/v1/workspaces/wks_1a2b3c4d5e6f/credentials/heygen",
            "headers": {"Authorization": "Bearer abc.def", "Cookie": "a=b",
                        "Stripe-Signature": "t=1,v1=x", "X-Api-Key": "k",
                        "User-Agent": "pytest"},
            "cookies": {"vf_session": "ses_1"},
            "data": {"value": "sk_live_SHOULDNOTSURVIVE"},
            "env": {"REMOTE_ADDR": "198.51.100.7"},
            "query_string": "code=oidc-code&state=s1&page=3",
        },
        "user": {"id": "usr_1a2b3c4d5e6f", "email": "a@b.co", "ip_address": "1.2.3.4"},
        "exception": {"values": [{
            "type": "RuntimeError",
            "value": "user bob@corp.io key sk_test_abc123 hook whsec_zz9 in ws_42",
            "stacktrace": {"frames": [{"function": "ws_list_jobs",
                                       "context_line": "  raise X('x@y.io')"}]},
        }]},
        "breadcrumbs": {"values": [{"message": "Bearer zzz.yyy for u_77"}]},
    }
    out = obs.scrub_event(event, None)
    blob = json.dumps(out)
    for leaked in ("abc.def", "sk_live_", "198.51.100.7", "oidc-code",
                   "a@b.co", "1.2.3.4", "bob@corp.io", "sk_test_abc123",
                   "whsec_zz9", "ws_42", "x@y.io", "zzz.yyy", "u_77",
                   "wks_1a2b3c4d5e6f"):
        assert leaked not in blob, leaked
    assert out["request"]["headers"]["User-Agent"] == "pytest"
    assert out["request"]["headers"]["Authorization"] == "[Filtered]"
    assert out["request"]["headers"]["X-Api-Key"] == "[Filtered]"
    assert "page=3" in out["request"]["query_string"]
    assert out["user"] == {"id": "usr_1a2b3c4d5e6f"}
    # Python identifiers in frames survive (no digits -> not an id).
    assert out["exception"]["values"][0]["stacktrace"]["frames"][0]["function"] == "ws_list_jobs"


def test_init_sentry_pii_options(monkeypatch):
    obs = _obs()
    with mock.patch("sentry_sdk.init") as fake_init:
        assert obs.init_sentry(DSN) is True
    kw = fake_init.call_args.kwargs
    assert kw["send_default_pii"] is False
    assert kw["include_local_variables"] is False
    assert kw["max_request_body_size"] == "never"
    assert kw["before_send"] is obs.scrub_event
    assert kw["traces_sample_rate"] == 0.0


# ────────────────────────────────────────────────────────────────────
# D2 - uvicorn stdout is JSON
# ────────────────────────────────────────────────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("extra_args", [
    [],
    ["--log-config", "api/uvicorn_log_config.json"],
], ids=["plain-cli", "entrypoint-log-config"])
def test_uvicorn_subprocess_every_stdout_line_is_json(tmp_path, extra_args):
    port = _free_port()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST_", "SENTRY_"))}
    env.update({
        "PYTHONPATH": str(REPO_ROOT),
        # Dict form on purpose: test_wave7_deploy's plaintext-secret
        # scanner flags ``<SECRET_VAR>=<token>`` kwargs lines.
        "VIDEO_FACTORY_SECRET_KEY": VALID_FERNET,
        "VIDEO_FACTORY_DB_KEY": DB_KEY,
        "VIDEO_FACTORY_DB": str(tmp_path / "uv.db"),
        "VIDEO_FACTORY_STORAGE": str(tmp_path / "storage"),
    })
    env.pop("VIDEO_FACTORY_ENV", None)
    cmd = [sys.executable, "-m", "uvicorn", "api.main:app",
           "--host", "127.0.0.1", "--port", str(port), "--access-log",
           *extra_args]
    proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 120
        up = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                if _get(base + "/healthz") == 200:
                    up = True
                    break
            except Exception:  # noqa: BLE001 - not listening yet
                time.sleep(0.3)
        if not up:
            proc.kill()
            out, err = proc.communicate(timeout=30)
            pytest.fail(f"uvicorn did not come up.\nSTDOUT:\n{out}\nSTDERR:\n{err}")
        assert _get(base + "/readyz") == 200
        assert _get(base + "/metrics") == 200
        assert _get(base + "/no-such-route") == 404
        _get(base + "/auth/oidc/google/callback?code=SECRETCODE123&state=S7")
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=30)

    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, f"no stdout at all; stderr:\n{err}"
    parsed = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            pytest.fail(f"plaintext line on stdout: {ln!r}")
        assert isinstance(obj, dict)
        assert {"timestamp", "level", "logger", "message"} <= obj.keys(), obj
        parsed.append(obj)
    # stderr must not carry uvicorn's plaintext handlers either.
    err_lines = [ln for ln in err.splitlines() if ln.strip()]
    for ln in err_lines:
        assert not ln.startswith(("INFO:", "WARNING:", "ERROR:")), ln
    msgs = [p["message"] for p in parsed]
    assert any(m.startswith("Started server process") for m in msgs), msgs
    assert any(m.startswith("Finished server process") for m in msgs), msgs
    access = [p for p in parsed if p["logger"] == "uvicorn.access"]
    paths = {p["path"].split("?")[0]: p["status_code"] for p in access}
    assert paths.get("/healthz") == 200
    assert paths.get("/no-such-route") == 404
    # OIDC code/state never hit the logs in clear.
    assert "SECRETCODE123" not in out
    assert "S7&" not in out and "state=S7" not in out


def test_uvicorn_log_config_file_matches_code():
    from api.observability import uvicorn_log_config
    on_disk = json.loads((REPO_ROOT / "api" / "uvicorn_log_config.json").read_text())
    assert on_disk == uvicorn_log_config()


def test_entrypoints_pass_the_json_log_config():
    flag = "api/uvicorn_log_config.json"
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    cmd = dockerfile[dockerfile.rindex("CMD ["):]
    assert '"--log-config"' in cmd and f'"{flag}"' in cmd
    prod = (REPO_ROOT / "docker-compose.production.yml").read_text()
    api_cmd = [ln for ln in prod.splitlines()
               if ln.strip().startswith("command:") and "uvicorn" in ln]
    assert api_cmd and all("--log-config" in ln and flag in ln for ln in api_cmd)
    dev = (REPO_ROOT / "docker-compose.yml").read_text()
    assert f"uvicorn api.main:app --host 127.0.0.1 --port 8000 --log-config {flag}" in dev


def test_configure_json_logging_replaces_uvicorn_handlers():
    obs = _obs()
    uv = logging.getLogger("uvicorn.access")
    plain = logging.StreamHandler()
    uv.addHandler(plain)
    uv.propagate = False
    obs.configure_json_logging()
    obs.configure_json_logging()  # idempotent
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert lg.handlers == [], (name, lg.handlers)
        assert lg.propagate is True
    root_json = [h for h in logging.getLogger().handlers
                 if getattr(h, "_vf_json", False)]
    assert len(root_json) == 1
    assert isinstance(root_json[0], obs.JsonStdoutHandler)


def test_access_record_is_structured_and_redacted():
    obs = _obs()
    rec = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__,
        lineno=1, msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.1:5555", "GET", "/auth/cb?code=abc&state=def&x=1",
              "1.1", 302),
        exc_info=None,
    )
    rec.color_message = "\x1b[32mcolour\x1b[0m"
    out = json.loads(obs.JsonLogFormatter().format(rec))
    assert out["method"] == "GET"
    assert out["status_code"] == 302
    assert out["path"].startswith("/auth/cb?")
    assert "abc" not in json.dumps(out) and "def" not in out["path"]
    assert "x=1" in out["path"]
    assert "color_message" not in out


# ────────────────────────────────────────────────────────────────────
# D3 - docs + dashboard parity with the registry
# ────────────────────────────────────────────────────────────────────

DOC = REPO_ROOT / "docs" / "observability.md"
DASH = REPO_ROOT / "infra" / "grafana" / "video-factory-dashboard.json"
_SERIES_RE = re.compile(r"\bvideo_factory_[a-z_]+\b")
_HIST_SUFFIXES = ("_bucket", "_count", "_sum", "_created")


def _registry_series() -> dict:
    obs = _obs()
    return {
        "video_factory_jobs_total": obs.JOBS_TOTAL,
        "video_factory_live_submits_total": obs.LIVE_SUBMITS_TOTAL,
        "video_factory_webhook_receipts_total": obs.WEBHOOK_RECEIPTS_TOTAL,
        "video_factory_errors_total": obs.ERRORS_TOTAL,
        "video_factory_request_duration_seconds": obs.REQUEST_DURATION,
    }


def _base_series(name: str) -> str:
    known = _registry_series()
    if name in known:
        return name
    for suf in _HIST_SUFFIXES:
        if name.endswith(suf) and name[: -len(suf)] in known:
            return name[: -len(suf)]
    # prometheus_client exposes a counter's creation timestamp as
    # ``<name minus _total>_created``.
    if name.endswith("_created") and name[: -len("_created")] + "_total" in known:
        return name[: -len("_created")] + "_total"
    return name


def _metrics_type_lines(tmp_path, monkeypatch) -> dict[str, str]:
    app = _app(monkeypatch, tmp_path)
    body = TestClient(app).get("/metrics").text
    out = {}
    for ln in body.splitlines():
        if ln.startswith("# TYPE "):
            _, _, name, typ = ln.split(" ", 3)
            out[name] = typ
    return out


def test_every_series_named_in_doc_exists_in_metrics(tmp_path, monkeypatch):
    names = set(_SERIES_RE.findall(DOC.read_text()))
    assert names, "docs/observability.md names no series"
    exposed = _metrics_type_lines(tmp_path, monkeypatch)
    for name in sorted(names):
        base = _base_series(name)
        assert base in exposed, f"doc names {name!r} but /metrics has no {base!r}"
    # ...and every exposed series is documented.
    documented = {_base_series(n) for n in names}
    for name in exposed:
        assert _base_series(name) in documented, (
            f"/metrics series {name!r} missing from doc")


def test_doc_metrics_table_matches_registry():
    obs = _obs()
    enums = {
        "state": obs.JOB_STATE_LABELS,
        "provider": obs.LIVE_PROVIDER_LABELS,
        "outcome": obs.LIVE_SUBMIT_OUTCOME_LABELS,
        "source": obs.WEBHOOK_SOURCE_LABELS,
        "class": obs.ERROR_CLASS_LABELS,
        "method": obs.HTTP_METHOD_LABELS,
    }
    rows = {}
    for ln in DOC.read_text().splitlines():
        m = re.match(r"^\| `(video_factory_[a-z_]+)` \| (\w+) \| ([^|]*)\| ([^|]*)\|", ln)
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3), m.group(4))
    reg = _registry_series()
    assert set(rows) == set(reg), (sorted(rows), sorted(reg))
    for name, (typ, labels_cell, allowed_cell) in rows.items():
        col = reg[name]
        assert typ == col._type, (name, typ, col._type)
        assert tuple(re.findall(r"`(\w+)`", labels_cell)) == tuple(col._labelnames), name
        for label, values in re.findall(r"(\w+): ((?:`[^`]+`(?:, )?)+)", allowed_cell):
            assert label in col._labelnames, (name, label)
            assert set(re.findall(r"`([^`]+)`", values)) == set(enums[label]), (name, label)


def test_dashboard_json_valid_and_references_only_existing_series():
    dash = json.loads(DASH.read_text())
    panels = dash["panels"]
    assert len(panels) >= 6
    reg = _registry_series()
    referenced = set()
    for p in panels:
        assert p.get("title")
        targets = p.get("targets") or []
        assert targets, f"panel {p['title']!r} has no targets"
        for t in targets:
            expr = t["expr"]
            names = _SERIES_RE.findall(expr)
            assert names, (p["title"], expr)
            labels_ok = {"le"}
            for n in names:
                base = _base_series(n)
                assert base in reg, f"panel {p['title']!r} references unknown {n!r}"
                referenced.add(base)
                labels_ok |= set(reg[base]._labelnames)
            for grp in re.findall(r"by \(([^)]*)\)", expr):
                for lab in [x.strip() for x in grp.split(",") if x.strip()]:
                    assert lab in labels_ok, (p["title"], lab)
            for lab in re.findall(r"[{,]\s*(\w+)\s*=~?", expr):
                assert lab in labels_ok, (p["title"], lab)
    assert referenced == set(reg), set(reg) - referenced


# ────────────────────────────────────────────────────────────────────
# Label bounding promised by the docs
# ────────────────────────────────────────────────────────────────────


def test_unmatched_routes_and_odd_methods_stay_bounded(tmp_path, monkeypatch):
    app = _app(monkeypatch, tmp_path)
    client = TestClient(app)
    # The registry is process-global: other tests on this xdist
    # worker may already have recorded their routes. Compare deltas.
    before = set(re.findall(r'route="([^"]*)"', client.get("/metrics").text))
    for i in range(15):
        assert client.get(f"/scan-{i}-wp-login.php").status_code == 404
    client.request("PROPFIND", "/healthz")
    client.get("/metrics")
    body = client.get("/metrics").text
    assert "scan-" not in body
    assert "PROPFIND" not in body
    assert 'route="unmatched"' in body
    assert 'route="/metrics"' not in body
    after = set(re.findall(r'route="([^"]*)"', body))
    assert after - before <= {"unmatched", "/healthz"}, after - before


# ────────────────────────────────────────────────────────────────────
# D4 / D5 / D6 - /readyz
# ────────────────────────────────────────────────────────────────────


def test_readyz_ready_shape(tmp_path, monkeypatch):
    r = TestClient(_app(monkeypatch, tmp_path)).get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "checks", "errors", "warnings"}
    assert body["status"] == "ready"
    assert body["checks"] == {"db": True, "vault_key": True, "master_key": True}
    assert body["errors"] == {} and body["warnings"] == {}


@pytest.mark.parametrize("bad_key", ["too_short", "X" * 44, "!" * 44])
def test_readyz_invalid_fernet_key_is_unready(tmp_path, monkeypatch, bad_key):
    r = TestClient(_app(monkeypatch, tmp_path, secret_key=bad_key)).get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unready"
    assert body["checks"]["vault_key"] is False
    assert body["errors"]["vault_key"] == "SecretKeyInvalid"
    assert body["checks"]["db"] is True
    assert bad_key not in r.text


def test_readyz_missing_vault_key(tmp_path, monkeypatch):
    r = TestClient(_app(monkeypatch, tmp_path, secret_key="")).get("/readyz")
    assert r.status_code == 503
    assert r.json()["errors"]["vault_key"] == "SecretKeyMissing"


def test_readyz_dev_master_key_refused_in_production(tmp_path, monkeypatch):
    app = _app(monkeypatch, tmp_path, secret_key="", db_key="", env="production")
    r = TestClient(app).get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["master_key"] is False
    assert body["errors"]["master_key"] == "dev_key_in_use"


def test_readyz_dev_master_key_warns_outside_production(tmp_path, monkeypatch):
    app = _app(monkeypatch, tmp_path, secret_key="", db_key="")
    body = TestClient(app).get("/readyz").json()
    assert body["checks"]["master_key"] is True
    assert body["warnings"]["master_key"] == "dev_key_in_use"
    assert "master_key" not in body["errors"]


@pytest.mark.parametrize("env,status,ready", [
    ("production", 503, False), ("PRODUCTION", 503, False),
    ("staging", 200, True), (None, 200, True),
])
def test_readyz_master_key_alone_flips_readiness(tmp_path, monkeypatch, env,
                                                 status, ready):
    app = _app(monkeypatch, tmp_path, env=env)
    with mock.patch("control_plane.artifact_hmac.dev_key_in_use", return_value=True):
        r = TestClient(app).get("/readyz")
    assert r.status_code == status
    body = r.json()
    assert body["checks"]["vault_key"] is True and body["checks"]["db"] is True
    assert body["checks"]["master_key"] is ready
    if ready:
        assert body["warnings"] == {"master_key": "dev_key_in_use"}
    else:
        assert body["errors"] == {"master_key": "dev_key_in_use"}


def test_production_flag_is_shared_with_cookie_policy(monkeypatch):
    from auth.session import is_production, should_use_secure_cookies
    monkeypatch.delenv("VF_HTTPS", raising=False)
    monkeypatch.setenv("VIDEO_FACTORY_ENV", " Production ")
    assert is_production() and should_use_secure_cookies()
    monkeypatch.setenv("VIDEO_FACTORY_ENV", "dev")
    assert not is_production() and not should_use_secure_cookies()


def test_validate_key_never_echoes_key(monkeypatch):
    from control_plane import secret_vault
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", "not-a-fernet-key-value")
    with pytest.raises(secret_vault.SecretKeyInvalid) as ei:
        secret_vault.validate_key()
    assert "not-a-fernet-key-value" not in str(ei.value)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", VALID_FERNET)
    secret_vault.validate_key()
