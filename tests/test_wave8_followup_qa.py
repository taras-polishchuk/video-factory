"""Wave 8 follow-up QA (t_9a6b3df4): independent adversarial tests.

Written by QA against branch wt/t_d967616e (bb1c5af), NOT by the
developer. Black-box first: most tests drive a real
``uvicorn`` subprocess and look only at HTTP responses, stdout/stderr
and the bytes a fake Sentry server receives.

Each test is tagged GUARD (behaviour that must keep holding, passes
today) or DEFECT <id> (pins a real defect; FAILS on bb1c5af and
should turn green when the defect is fixed). Defect ids are listed in
artifacts/t_9a6b3df4-wave8-followup-qa.md.

    DEFECT QA-N1  scrub_text / scrub_event are quadratic (ReDoS) on a long
                  alnum run; they run on the event loop inside the 500 handler.
    DEFECT QA-N2  /readyz db check is `SELECT 1` on a cached connection;
                  it never touches the database file.
    DEFECT QA-N3  percent-encoded email under a neutral query key reaches Sentry.
    DEFECT QA-N4  /metrics emits `*_created` series that docs/observability.md
                  does not list.
    DEFECT QA-N5  redact_query_string misses alias keys and nested secrets.
    DEFECT QA-N6  request_id_from accepts a trailing newline (``$`` vs fullmatch).
    DEFECT QA-N7  docker-compose.production.yml never forwards SENTRY_DSN /
                  VIDEO_FACTORY_ENV to the api container.

All secrets / emails below are synthetic.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

VALID_FERNET = "a" * 43 + "="
DB_KEY = "qa-db-key-0123456789abcdef"
SESSION_SECRET = "qa-session-secret-0123456789abcdef0123456789abcdef"


# ────────────────────────────────────────────────────────────────────
# live-uvicorn helpers
# ────────────────────────────────────────────────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


WRAPPER_APP = textwrap.dedent('''
    """QA wrapper: the real api.main:app plus routes that raise."""
    from fastapi import Request
    from api.main import app


    @app.get("/__qa/boom")
    async def _boom():
        raise RuntimeError("boom for alice@example.com id usr_abc123def456")


    @app.post("/__qa/boom_post")
    async def _boom_post(request: Request):
        await request.body()
        raise KeyError("carol@example.com")
''')


class LiveServer:
    """A real ``python -m uvicorn`` subprocess on a free port."""

    def __init__(self, tmp_path: Path, *, env: dict | None = None,
                 drop: tuple = (), app: str = "api.main:app",
                 uvicorn_args: tuple = ("--log-config",
                                        "api/uvicorn_log_config.json")):
        self.tmp = tmp_path
        self.port = _free_port()
        (tmp_path / "qa_boom_app.py").write_text(WRAPPER_APP)
        e = dict(os.environ)
        for k in ("VIDEO_FACTORY_ENV", "SENTRY_DSN", "VF_HTTPS",
                  "VIDEO_FACTORY_SECRET_KEY", "VIDEO_FACTORY_DB_KEY"):
            e.pop(k, None)
        # dict form on purpose: tests/test_wave7_deploy.py greps the whole
        # repo for ``VIDEO_FACTORY_*_KEY=<value>`` and fails on kwargs style.
        e.update({
            "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{tmp_path}",
            "PYTHONUNBUFFERED": "1",
            "VIDEO_FACTORY_DB": str(tmp_path / "vf.db"),
            "VIDEO_FACTORY_STORAGE": str(tmp_path / "storage"),
            "VIDEO_FACTORY_SECRET_KEY": VALID_FERNET,
            "VIDEO_FACTORY_DB_KEY": DB_KEY,
            "VIDEO_FACTORY_SESSION_SECRET": SESSION_SECRET,
        })
        e.update(env or {})
        for k in drop:
            e.pop(k, None)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1",
             "--port", str(self.port), "--access-log", *uvicorn_args],
            cwd=str(REPO_ROOT), env=e, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                if self.req("GET", "/healthz")[0] == 200:
                    return
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        out, err = self.stop()
        raise AssertionError(f"uvicorn did not come up\nstdout={out}\nstderr={err}")

    def req(self, method, path, body=None, headers=None, timeout=20):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, data, hdrs

    def stop(self, sig=signal.SIGINT):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            out, err = self.proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
        return out, err


@pytest.fixture
def live(tmp_path):
    started: list[LiveServer] = []

    def _start(**kw):
        s = LiveServer(tmp_path, **kw)
        started.append(s)
        return s

    yield _start
    for s in started:
        if s.proc.poll() is None:
            s.stop()


class FakeSentry:
    """Local HTTP server standing in for a Sentry ingest endpoint."""

    def __init__(self):
        self.envelopes: list[bytes] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length", 0))
                body = self.rfile.read(n)
                if self.headers.get("content-encoding") == "gzip":
                    body = gzip.decompress(body)
                outer.envelopes.append(body)
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):  # noqa: D401
                pass

        self.port = _free_port()
        self.srv = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def dsn(self) -> str:
        return f"http://qapublickey@127.0.0.1:{self.port}/1"

    def events(self) -> list[dict]:
        out = []
        for env in self.envelopes:
            lines = env.split(b"\n")
            i = 1
            while i < len(lines) - 1:
                try:
                    hdr = json.loads(lines[i])
                except ValueError:
                    break
                if hdr.get("type") == "event":
                    out.append(json.loads(lines[i + 1]))
                i += 2
        return out

    def raw(self) -> str:
        return b"\n".join(self.envelopes).decode("utf-8", "replace")

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def fake_sentry():
    fs = FakeSentry()
    yield fs
    fs.close()


def _json_lines(text: str) -> tuple[list[dict], list[str]]:
    good, bad = [], []
    for line in text.split("\n"):
        if not line:
            continue
        try:
            obj = json.loads(line)
            assert isinstance(obj, dict)
            good.append(obj)
        except Exception:  # noqa: BLE001
            bad.append(line)
    return good, bad


# ────────────────────────────────────────────────────────────────────
# D1 black-box (GUARD)
# ────────────────────────────────────────────────────────────────────


def test_qa_guard_live_500_sends_exactly_one_scrubbed_sentry_event(
        live, fake_sentry):
    """GUARD D1. Real uvicorn, real HTTP Sentry ingest on localhost.

    One unhandled exception -> exactly ONE event envelope (no duplicate
    from the logging / asgi integrations). Non-500 statuses -> none.
    No cookie / Authorization / client IP / body / email in any byte
    the server received.
    """
    s = live(app="qa_boom_app:app", env={"SENTRY_DSN": fake_sentry.dsn})
    assert s.req("GET", "/nope")[0] == 404
    assert s.req("GET", "/api/v1/workspaces/wks_x/jobs")[0] == 401
    sentinels = {
        "cookie": "COOKIESENTINEL991", "bearer": "BEARERSENTINEL992",
        "ip": "203.0.113.77", "hdr_email": "bob@example.com",
        "apikey": "APIKEYSENTINEL993", "code": "OIDCCODESENTINEL994",
        "exc_email": "alice@example.com", "body_email": "carol@example.com",
        "body_pw": "PASSWORDSENTINEL996", "pathid": "usr_abc123def456",
    }
    st, body, hdrs = s.req(
        "GET", f"/__qa/boom?code={sentinels['code']}&q=1",
        headers={
            "Cookie": f"vf_session={sentinels['cookie']}",
            "Authorization": f"Bearer {sentinels['bearer']}",
            "X-Forwarded-For": sentinels["ip"],
            "X-User-Email": sentinels["hdr_email"],
            "X-Api-Key": sentinels["apikey"],
            "X-Request-ID": "qa-req-0001abcd",
        })
    assert st == 500
    assert json.loads(body) == {"error": "internal server error",
                                "request_id": "qa-req-0001abcd"}
    assert hdrs["x-request-id"] == "qa-req-0001abcd"
    st, body, _ = s.req(
        "POST", "/__qa/boom_post",
        body=json.dumps({"email": sentinels["body_email"],
                         "password": sentinels["body_pw"]}),
        headers={"Content-Type": "application/json",
                 "Cookie": f"vf_session={sentinels['cookie']}"})
    assert st == 500
    time.sleep(2.5)  # let the SDK's background worker flush
    out, err = s.stop()
    events = fake_sentry.events()
    assert len(events) == 2, (
        f"expected one event per 500 (2 total), got {len(events)}")
    assert {e["tags"]["request_id"] for e in events} >= {"qa-req-0001abcd"}
    raw = fake_sentry.raw()
    leaked = {k: v for k, v in sentinels.items() if v in raw}
    assert not leaked, f"PII/secret sentinels reached Sentry: {leaked}"


# ────────────────────────────────────────────────────────────────────
# D2 black-box (GUARD)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("uvicorn_args,sig", [
    (("--log-config", "api/uvicorn_log_config.json"), signal.SIGINT),
    (("--log-config", "api/uvicorn_log_config.json"), signal.SIGTERM),
    ((), signal.SIGINT),
    (("--log-config", "api/uvicorn_log_config.json", "--workers", "2"),
     signal.SIGINT),
], ids=["logcfg-sigint", "logcfg-sigterm", "plain-cli", "logcfg-2workers"])
def test_qa_guard_live_stdout_100pct_json_and_no_log_forging(
        live, uvicorn_args, sig):
    """GUARD D2. Startup + requests (incl. a 500, a garbage TCP probe and
    a log-forging path) + shutdown. Every stdout line is a JSON object;
    the forged ``%0A{...}`` path must not produce a forged record.
    """
    s = live(app="qa_boom_app:app", uvicorn_args=uvicorn_args)
    s.req("GET", "/readyz")
    s.req("GET", "/nope?code=SECRETCODE&x=1")
    s.req("GET", "/__qa/boom")
    forged = '{"timestamp":"x","level":"CRITICAL","logger":"forged","message":"FORGED"}'
    s.req("GET", "/x%0A" + forged.replace('"', "%22"))
    sock = socket.create_connection(("127.0.0.1", s.port), timeout=3)
    sock.sendall(b"\x16\x03\x01not http at all\r\n\r\n")
    time.sleep(0.4)
    sock.close()
    time.sleep(0.3)
    out, err = s.stop(sig)
    good, bad = _json_lines(out)
    assert not bad, f"non-JSON stdout lines: {bad[:3]}"
    assert len(good) >= 8
    assert "SECRETCODE" not in out, "OIDC-style code leaked into stdout logs"
    assert not [g for g in good if g.get("logger") == "forged"
                or g.get("message") == "FORGED"], "log forging succeeded"
    for g in good:
        assert {"timestamp", "level", "logger", "message"} <= set(g), g


# ────────────────────────────────────────────────────────────────────
# D4-D6 black-box (GUARD)
# ────────────────────────────────────────────────────────────────────


def _b64_key(n: int) -> str:
    import base64
    return base64.urlsafe_b64encode(b"x" * n).decode()


@pytest.mark.parametrize("env,drop,want_status,want_error", [
    ({}, (), 200, None),
    ({"VIDEO_FACTORY_SECRET_KEY": "too_short"}, (), 503, ("vault_key", "SecretKeyInvalid")),
    ({"VIDEO_FACTORY_SECRET_KEY": _b64_key(31)}, (), 503, ("vault_key", "SecretKeyInvalid")),
    ({"VIDEO_FACTORY_SECRET_KEY": _b64_key(33)}, (), 503, ("vault_key", "SecretKeyInvalid")),
    ({"VIDEO_FACTORY_SECRET_KEY": "\u043a\u043b\u044e\u0447" * 11}, (), 503, ("vault_key", "SecretKeyInvalid")),
    ({"VIDEO_FACTORY_SECRET_KEY": "   "}, (), 503, ("vault_key", "SecretKeyMissing")),
    ({"VIDEO_FACTORY_ENV": " PRODUCTION "},
     ("VIDEO_FACTORY_DB_KEY", "VIDEO_FACTORY_SECRET_KEY"), 503, ("master_key", "dev_key_in_use")),
    ({"VIDEO_FACTORY_ENV": "production", "VIDEO_FACTORY_DB_KEY": "   "},
     ("VIDEO_FACTORY_SECRET_KEY",), 503, ("master_key", "dev_key_in_use")),
    ({"VIDEO_FACTORY_ENV": "production"}, (), 200, None),
], ids=["healthy", "too_short", "31B", "33B", "non-ascii", "blank-key",
        "prod-upper-ws-devkey", "prod-blank-dbkey-devkey", "prod-healthy"])
def test_qa_guard_live_readyz_key_matrix(live, env, drop, want_status, want_error):
    """GUARD D4/D5/D6. Body shape + status for hostile key configs.

    Also asserts the response never echoes key material.
    """
    if env.get("VIDEO_FACTORY_DB_KEY") == "   ":
        # Repo() would refuse a blank key on boot; skip the boot-time DB
        # key and use only the HMAC-key resolution path.
        drop = tuple(drop) + ("VIDEO_FACTORY_DB_KEY",)
        env = {k: v for k, v in env.items() if k != "VIDEO_FACTORY_DB_KEY"}
    s = live(env=env, drop=drop)
    st, body, _ = s.req("GET", "/readyz")
    j = json.loads(body)
    assert st == want_status, body
    assert set(j) == {"status", "checks", "errors", "warnings"}, j
    assert all(isinstance(v, bool) for v in j["checks"].values()), j
    assert j["status"] == ("ready" if want_status == 200 else "unready")
    if want_error:
        check, code = want_error
        assert j["checks"][check] is False, j
        assert j["errors"][check] == code, j
    for secret in (env.get("VIDEO_FACTORY_SECRET_KEY") or "-", DB_KEY, VALID_FERNET):
        if secret.strip():
            assert secret not in body, "readyz echoed key material"
    out, _ = s.stop()
    assert _json_lines(out)[1] == []


# ────────────────────────────────────────────────────────────────────
# D3 bounded cardinality (GUARD, in-process)
# ────────────────────────────────────────────────────────────────────


def test_qa_guard_metrics_cardinality_bounded_under_scan(monkeypatch, tmp_path):
    """GUARD D3. 300 distinct 404 paths x 12 odd HTTP methods must not
    mint more than a handful of request_duration series.
    """
    from fastapi.testclient import TestClient
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", VALID_FERNET)
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", DB_KEY)
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    from api import observability as obs
    from api.main import create_app
    client = TestClient(create_app(), raise_server_exceptions=False)
    before = {s.labels.get("route") for m in obs.REGISTRY.collect()
              for s in m.samples if m.name == "video_factory_request_duration_seconds"}
    for i in range(300):
        client.get(f"/scan-{i}/.env?x={i}")
    for m in ("PROPFIND", "MKCOL", "FOO", "BREW", "PURGE", "LOCK", "UNLOCK",
              "COPY", "MOVE", "TRACE", "CONNECT", "SEARCH"):
        client.request(m, "/healthz")
    after = {(s.labels.get("route"), s.labels.get("method"))
             for m in obs.REGISTRY.collect()
             for s in m.samples if m.name == "video_factory_request_duration_seconds_count"}
    new_routes = {r for r, _ in after} - before
    assert len(new_routes) <= 3, new_routes
    assert {m for _, m in after} <= {"GET", "POST", "PUT", "PATCH", "DELETE",
                                     "HEAD", "OPTIONS", "OTHER"}


# ────────────────────────────────────────────────────────────────────
# DEFECT QA-N1: quadratic scrubbing on the 500 path
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n", [32000])
def test_qa_defect_n1_scrub_is_linear_on_long_alnum_run(n):
    """DEFECT QA-N1. ``_EMAIL_RE`` is ``[A-Za-z0-9._%+-]+@...``: with no
    ``@`` in the input every start offset rescans the whole run, so cost
    is O(n^2). Measured on bb1c5af: 16 KB (about the largest request
    line uvicorn/h11 accepts) 0.4-1.3 s, 32 KB 1.6-4.5 s, 64 KB 17 s;
    linear code takes < 10 ms. The test uses 32 KB so a fast CPU cannot
    hide the quadratic term. The scrubber runs on
    the event loop (scope event processor inside the async 500 handler),
    so one crafted request to any 500 route stalls every other request.
    """
    from api import observability as obs
    payload = "a" * n
    t0 = time.perf_counter()
    obs.scrub_text(payload)
    t_text = time.perf_counter() - t0
    event = {"request": {"query_string": "x=" + payload, "headers": {},
                         "url": "http://h/p"},
             "exception": {"values": [{"type": "E", "value": "boom"}]}}
    t0 = time.perf_counter()
    obs.scrub_event(event)
    t_event = time.perf_counter() - t0
    assert t_text < 0.5, f"scrub_text({n}) took {t_text:.2f}s (quadratic)"
    assert t_event < 0.5, f"scrub_event({n}) took {t_event:.2f}s (quadratic)"


# ────────────────────────────────────────────────────────────────────
# DEFECT QA-N2: /readyz db check never touches the DB
# ────────────────────────────────────────────────────────────────────


def _lock_db(tmp_path: Path) -> subprocess.Popen:
    code = textwrap.dedent(f"""
        import time, core.cipher_db as c
        conn = c.connect({str(tmp_path / 'vf.db')!r}, key={DB_KEY!r})
        conn.execute('PRAGMA busy_timeout=0')
        conn.execute('BEGIN EXCLUSIVE')
        print('locked', flush=True)
        time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                         env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    return p


@pytest.mark.parametrize("fault", ["write-locked", "file-deleted"])
def test_qa_defect_n2_readyz_503_on_real_db_failure(live, tmp_path, fault):
    """DEFECT QA-N2. The probe is ``repo._conn().execute("SELECT 1")`` on
    the per-thread cached connection; SELECT 1 does not read the file, so
    a DB that another process holds write-locked (real writes -> 500 after
    the 5 s busy timeout) or whose file was unlinked still reports 200.
    The existing test only mocks ``_conn`` to raise.
    """
    s = live()
    body = json.dumps({"email": "qa1@example.com", "password": "QaPassw0rdLong12",
                       "workspace_name": "QA WS 1"})
    hdr = {"Content-Type": "application/json"}
    assert s.req("POST", "/auth/signup", body, hdr)[0] == 200
    assert s.req("GET", "/readyz")[0] == 200
    locker = None
    try:
        if fault == "write-locked":
            locker = _lock_db(tmp_path)
            body2 = body.replace("qa1@", "qa2@")
            st, _, _ = s.req("POST", "/auth/signup", body2, hdr, timeout=30)
            assert st >= 500, "precondition: a real write must fail while locked"
        else:
            for suf in ("", "-wal", "-shm"):
                try:
                    os.unlink(f"{tmp_path}/vf.db{suf}")
                except FileNotFoundError:
                    pass
        st, rbody, _ = s.req("GET", "/readyz")
        j = json.loads(rbody)
        assert st == 503 and j["checks"]["db"] is False, (
            f"/readyz said {st} {rbody} while the DB is unusable ({fault})")
    finally:
        if locker:
            locker.kill()


# ────────────────────────────────────────────────────────────────────
# DEFECT QA-N3 / QA-N5: query-string redaction gaps
# ────────────────────────────────────────────────────────────────────


def test_qa_defect_n3_percent_encoded_email_not_sent_to_sentry(live, fake_sentry):
    """DEFECT QA-N3. ``?contact=alice%40example.com`` (neutral key, so the
    key-name redaction does not fire) reaches Sentry verbatim in
    ``request.query_string``: the email regex cannot see ``%40``.
    """
    s = live(app="qa_boom_app:app", env={"SENTRY_DSN": fake_sentry.dsn})
    assert s.req("GET", "/__qa/boom?contact=alice%40leakcheck.example&q=1")[0] == 500
    time.sleep(2.5)
    s.stop()
    raw = fake_sentry.raw()
    assert fake_sentry.events(), "no event captured"
    assert "alice%40leakcheck.example" not in raw
    assert "alice@leakcheck.example" not in raw


@pytest.mark.parametrize("qs,secret", [
    ("jwt=QAJWT.SECRET.SIG", "QAJWT.SECRET.SIG"),
    ("pwd=hunter2hunter2", "hunter2hunter2"),
    ("otp=918273", "918273"),
    ("login_hint=bob%40leakcheck.example", "leakcheck"),
    ("next=%2Fcb%3Fcode%3DNESTEDSECRET", "NESTEDSECRET"),
], ids=["jwt", "pwd", "otp", "login_hint", "nested-code"])
def test_qa_defect_n5_redact_query_string_alias_and_nested(qs, secret):
    """DEFECT QA-N5. Key-name allow-list misses common aliases (jwt, pwd,
    otp, login_hint) and a secret smuggled inside another value
    (``next=/cb?code=...``). Used for both the access log and Sentry.
    """
    from api.observability import redact_query_string
    assert secret not in redact_query_string(qs)


# ────────────────────────────────────────────────────────────────────
# DEFECT QA-N4: docs vs /metrics, reverse direction
# ────────────────────────────────────────────────────────────────────


def test_qa_defect_n4_docs_list_every_emitted_series(monkeypatch, tmp_path):
    """DEFECT QA-N4. Task D3: docs list every series /metrics emits and
    nothing else. After one 500, /metrics also emits
    ``video_factory_errors_created`` (a gauge). The doc only mentions
    ``_created`` for the histogram, and the dev test only checks the
    forward direction (doc -> registry).
    """
    from api import observability as obs
    obs.ERRORS_TOTAL.labels(**{"class": "runtime"}).inc()
    obs.JOBS_TOTAL.labels(state="done").inc()
    text = obs.metrics_response().decode()
    emitted = set(re.findall(r"^# TYPE (\S+) ", text, flags=re.M))
    emitted |= {m for m in re.findall(r"^([A-Za-z_:][A-Za-z0-9_:]*)[{ ]", text, flags=re.M)}
    doc = (REPO_ROOT / "docs" / "observability.md").read_text()
    undocumented = sorted(
        n for n in emitted
        if n not in doc
        and not re.search(r"counter[^\n]{0,200}_created|_created[^\n]{0,200}counter",
                          doc, flags=re.I)
        # histogram sample suffixes are documented as a group
        and not re.search(r"_(bucket|count|sum)$", n))
    assert not undocumented, f"emitted but not documented: {undocumented}"


# ────────────────────────────────────────────────────────────────────
# DEFECT QA-N6: request-id regex
# ────────────────────────────────────────────────────────────────────


def test_qa_defect_n6_request_id_rejects_trailing_newline():
    """DEFECT QA-N6 (cosmetic). ``re.match(r"^...$")`` lets ``$`` match
    before a trailing ``\\n``, so a value with a newline is reused as-is
    (header / log-line splitting hygiene). Not reachable over HTTP/1.1
    today (the parser rejects it) but the validator is wrong.
    """
    from api.observability import request_id_from
    got = request_id_from("abcdefgh\n")
    assert "\n" not in got


# ────────────────────────────────────────────────────────────────────
# DEFECT QA-N7: shipped production compose cannot enable D1 / D6
# ────────────────────────────────────────────────────────────────────


def test_qa_defect_n7_production_compose_forwards_observability_env():
    """DEFECT QA-N7. docs/observability.md tells operators to set
    ``SENTRY_DSN`` and ``VIDEO_FACTORY_ENV=production``. The api service in
    docker-compose.production.yml has an explicit ``environment:`` list and
    no ``env_file:``; ``--env-file .env.production`` only feeds compose
    interpolation, so neither variable can reach the container.
    """
    text = (REPO_ROOT / "docker-compose.production.yml").read_text()
    m = re.search(r"^  api:\n(.*?)^  web:\n", text, flags=re.S | re.M)
    assert m, "api service block not found"
    api_block = m.group(1)
    has_env_file = re.search(r"^\s+env_file:", api_block, flags=re.M)
    for var in ("SENTRY_DSN", "VIDEO_FACTORY_ENV"):
        assert has_env_file or re.search(rf"^\s+{var}:", api_block, flags=re.M), (
            f"{var} is documented but not forwarded to the api container")
