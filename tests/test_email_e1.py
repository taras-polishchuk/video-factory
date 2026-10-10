"""EMAIL E1: verification + password-reset mail delivery.

Source: staging report VF-STG-08, audit A2 section 7 (no email transport
existed: verification and reset tokens were never delivered).

Covers:
- SMTP delivery through a real in-process SMTP server
  (tests/fake_smtp.py): plain, STARTTLS + AUTH, implicit TLS, all with
  real certificate verification via SMTP_CA_FILE.
- The mailed links work end to end: /verify/{token} -> POST
  /auth/verify, /reset?token= -> POST /auth/forgot/consume -> login.
- SMTP down / transient / permanent failures: the request still
  answers 200 at once, retries happen in the background, the outcome
  is audited as email.sent / email.failed without the token.
- No SMTP in production: "email not configured" log, no token in any
  response, log line, stderr/stdout or audit row.
- Dev console transport redacts the token unless the dev opt-in is set.
- /readyz carries email: configured | not_configured as info only.
- Reset page (web/src/routes/reset) exists and is wired like the other
  pre-session pages.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tests.fake_smtp import FakeSMTPServer, closed_port, make_tls_material

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "Password12345!"
NEW_PASSWORD = "NewPassword6789!"
BASE = "https://app.example.test"
ORIGIN = {"Origin": BASE}

_ENV_TO_CLEAR = (
    "VIDEO_FACTORY_ENV", "VF_HTTPS", "VIDEO_FACTORY_REQUIRE_ORIGIN",
    "VIDEO_FACTORY_PUBLIC_BASE_URL", "VIDEO_FACTORY_CORS_ORIGINS",
    "VIDEO_FACTORY_TRUSTED_PROXIES", "VIDEO_FACTORY_DEV_EXPOSE_TOKENS",
    "VIDEO_FACTORY_DB_KEY", "SENTRY_DSN",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
    "SMTP_TLS", "SMTP_TIMEOUT_SEC", "SMTP_CA_FILE", "SMTP_MAX_ATTEMPTS",
    "SMTP_RETRY_BACKOFF_SEC", "SMTP_QUEUE_MAX",
    "VF_SIGNUP_IP_LIMIT", "VF_FORGOT_IP_LIMIT", "VF_FORGOT_EMAIL_LIMIT",
    "VF_RESET_CONSUME_IP_LIMIT", "VF_VERIFY_IP_LIMIT",
)


@pytest.fixture()
def tls_files(tmp_path_factory):
    return make_tls_material(tmp_path_factory.mktemp("smtp-tls"))


@pytest.fixture()
def make_app(tmp_path, monkeypatch):
    for k in _ENV_TO_CLEAR:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(tmp_path / "storage"))

    def _make(*, env="test", **extra):
        if env is not None:
            monkeypatch.setenv("VIDEO_FACTORY_ENV", env)
        for k, v in extra.items():
            monkeypatch.setenv(k, str(v))
        from api.main import create_app
        from core.repo import Repo
        from core.runners import JobRunner, Singleton
        repo = Repo(str(tmp_path / "vf.db"))
        runner = JobRunner(repo=repo, storage_root=str(tmp_path / "storage"),
                           schema={}, budget_per_video_usd=5.0,
                           budget_per_batch_usd=20.0)
        Singleton.set(runner)
        app = create_app(repo=repo, runner=runner,
                         storage_root=str(tmp_path / "storage"), schema={})
        return app, repo
    return _make


def _smtp_env(srv: FakeSMTPServer, **over) -> dict:
    env = {
        "SMTP_HOST": "127.0.0.1", "SMTP_PORT": srv.port,
        "SMTP_FROM": "Video Factory <no-reply@example.test>",
        "SMTP_TLS": "none", "SMTP_TIMEOUT_SEC": 5,
        "SMTP_RETRY_BACKOFF_SEC": 0,
        "VIDEO_FACTORY_PUBLIC_BASE_URL": BASE,
    }
    env.update(over)
    return env


def _signup(c, email="alice@example.com", headers=None):
    return c.post("/auth/signup", json={
        "email": email, "password": PASSWORD, "workspace_name": "Acme",
    }, headers=headers or {})


def _email_audit(repo) -> list[dict]:
    rows = repo._conn().execute(
        "SELECT event, context_json, user_id FROM audit_log "
        "WHERE event LIKE 'email.%' ORDER BY created_at, rowid").fetchall()
    return [{"event": r[0], "context": json.loads(r[1] or "{}"),
             "user_id": r[2]} for r in rows]


def _bodies(msg) -> tuple[str, str]:
    text = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    return text, html


def _db_tokens(repo) -> list[str]:
    c = repo._conn()
    return ([r[0] for r in c.execute("SELECT token FROM email_verifications")]
            + [r[0] for r in c.execute("SELECT token FROM password_resets")])


# ── end-to-end delivery with working links ───────────────────────


def test_signup_mails_verify_link_that_verifies(make_app):
    with FakeSMTPServer() as srv:
        app, repo = make_app(**_smtp_env(srv))
        c = TestClient(app)
        r = _signup(c)
        assert r.status_code == 200
        assert "verification_token" not in r.text
        assert app.state.mailer.drain(10)
        assert len(srv.messages) == 1
        m = srv.messages[0]
        assert m["mail_from"] == "no-reply@example.test"
        assert m["rcpt_to"] == ["alice@example.com"]
        msg = srv.parsed()
        assert msg["To"] == "alice@example.com"
        assert msg["From"] == "Video Factory <no-reply@example.test>"
        assert "Verify your email" in msg["Subject"]
        assert msg["Message-ID"] and msg["Date"]
        text, html = _bodies(msg)
        link = re.search(r"https://app\.example\.test/verify/([A-Za-z0-9_-]+)", text)
        assert link, text
        token = link.group(1)
        assert f'href="{BASE}/verify/{token}"' in html
        assert _db_tokens(repo) == [token]
        # the mailed link works
        v = c.post("/auth/verify", json={"token": token})
        assert v.status_code == 200 and v.json()["email_verified"] is True
        assert c.get("/auth/me").json()["user"]["email_verified"] in (1, True)
        audit = _email_audit(repo)
        assert [a["event"] for a in audit] == ["email.sent"]
        assert audit[0]["context"]["kind"] == "verify"
        assert audit[0]["context"]["transport"] == "smtp"
        assert audit[0]["context"]["attempts"] == 1
        assert audit[0]["user_id"] == r.json()["user_id"]
        blob = json.dumps(audit)
        assert token not in blob and "alice@example.com" not in blob


def test_forgot_mails_reset_link_that_resets_password(make_app):
    with FakeSMTPServer() as srv:
        app, repo = make_app(**_smtp_env(srv))
        c = TestClient(app)
        assert _signup(c).status_code == 200
        assert app.state.mailer.drain(10)
        r = c.post("/auth/forgot", json={"email": "Alice@Example.com"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert app.state.mailer.drain(10)
        assert len(srv.messages) == 2
        msg = srv.parsed(1)
        assert "Reset your" in msg["Subject"]
        assert srv.messages[1]["rcpt_to"] == ["alice@example.com"]
        text, html = _bodies(msg)
        link = re.search(r"https://app\.example\.test/reset\?token=([A-Za-z0-9_-]+)", text)
        assert link, text
        token = link.group(1)
        assert f'href="{BASE}/reset?token={token}"' in html
        assert token not in r.text
        ok = c.post("/auth/forgot/consume",
                    json={"token": token, "new_password": NEW_PASSWORD})
        assert ok.status_code == 200
        c2 = TestClient(app)
        assert c2.post("/auth/login", json={
            "email": "alice@example.com", "password": PASSWORD}).status_code == 401
        assert c2.post("/auth/login", json={
            "email": "alice@example.com", "password": NEW_PASSWORD}).status_code == 200
        # one-shot link
        again = c.post("/auth/forgot/consume",
                       json={"token": token, "new_password": "Another12345!x"})
        assert again.status_code == 400
        events = [(a["event"], a["context"]["kind"]) for a in _email_audit(repo)]
        assert events == [("email.sent", "verify"), ("email.sent", "reset")]


def test_forgot_unknown_email_sends_nothing(make_app):
    with FakeSMTPServer() as srv:
        app, repo = make_app(**_smtp_env(srv))
        c = TestClient(app)
        r = c.post("/auth/forgot", json={"email": "nobody@example.com"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert app.state.mailer.drain(10)
        assert srv.connections == 0 and _email_audit(repo) == []


def test_starttls_with_auth_verifies_certificate(make_app, tls_files):
    with FakeSMTPServer(mode="starttls", tls_files=tls_files,
                        auth=("mailer", "pw-123")) as srv:
        app, repo = make_app(**_smtp_env(
            srv, SMTP_TLS="starttls", SMTP_USER="mailer",
            SMTP_PASSWORD="pw-123", SMTP_CA_FILE=tls_files["ca"]))
        assert app.state.mailer.status()["email"] == "configured"
        assert _signup(TestClient(app)).status_code == 200
        assert app.state.mailer.drain(10)
        assert len(srv.messages) == 1
        assert srv.messages[0]["tls"] is True
        assert srv.messages[0]["auth_user"] == "mailer"
        # AUTH happened only after the TLS upgrade
        cmds = [x.split()[0].upper() for x in srv.commands]
        assert cmds.index("STARTTLS") < cmds.index("AUTH")


def test_implicit_tls_port(make_app, tls_files):
    with FakeSMTPServer(mode="ssl", tls_files=tls_files,
                        auth=("mailer", "pw-123")) as srv:
        app, repo = make_app(**_smtp_env(
            srv, SMTP_TLS="ssl", SMTP_USER="mailer", SMTP_PASSWORD="pw-123",
            SMTP_CA_FILE=tls_files["ca"]))
        assert _signup(TestClient(app)).status_code == 200
        assert app.state.mailer.drain(10)
        assert len(srv.messages) == 1 and srv.messages[0]["tls"] is True


def test_untrusted_certificate_is_not_retried_and_nothing_sent(make_app, tls_files):
    # No SMTP_CA_FILE: the throw-away CA is not trusted -> refuse.
    with FakeSMTPServer(mode="starttls", tls_files=tls_files,
                        auth=("mailer", "pw-123")) as srv:
        app, repo = make_app(**_smtp_env(
            srv, SMTP_TLS="starttls", SMTP_USER="mailer",
            SMTP_PASSWORD="pw-123"))
        assert _signup(TestClient(app)).status_code == 200
        assert app.state.mailer.drain(10)
        assert srv.messages == []
        assert not any(x.startswith("AUTH") for x in srv.commands)
        (a,) = _email_audit(repo)
        assert a["event"] == "email.failed"
        assert a["context"]["error"] == "SSLCertVerificationError"
        assert a["context"]["attempts"] == 1


def test_starttls_not_offered_never_falls_back_to_plaintext(make_app, tls_files):
    with FakeSMTPServer(mode="starttls", tls_files=tls_files,
                        auth=("mailer", "pw-123"), offer_starttls=False) as srv:
        app, repo = make_app(**_smtp_env(
            srv, SMTP_TLS="starttls", SMTP_USER="mailer",
            SMTP_PASSWORD="pw-123", SMTP_CA_FILE=tls_files["ca"]))
        assert _signup(TestClient(app)).status_code == 200
        assert app.state.mailer.drain(10)
        assert srv.messages == []
        assert not any(x.startswith("AUTH") for x in srv.commands)
        (a,) = _email_audit(repo)
        assert a["event"] == "email.failed"
        assert a["context"]["error"] == "SMTPNotSupportedError"


# ── failures never reach the request ─────────────────────────────


def test_smtp_down_request_still_200_fast_and_audit_failed(make_app, caplog):
    port = closed_port()
    app, repo = make_app(
        SMTP_HOST="127.0.0.1", SMTP_PORT=port, SMTP_TLS="none",
        SMTP_FROM="no-reply@example.test", SMTP_TIMEOUT_SEC=2,
        SMTP_RETRY_BACKOFF_SEC=1, VIDEO_FACTORY_PUBLIC_BASE_URL=BASE)
    c = TestClient(app)
    assert _signup(c).status_code == 200
    assert app.state.mailer.drain(15)
    caplog.set_level(logging.DEBUG)
    t0 = time.monotonic()
    r = c.post("/auth/forgot", json={"email": "alice@example.com"})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200 and r.json() == {"ok": True}
    # 3 attempts with 1 s + 2 s backoff run in the background; the
    # request did not wait for them.
    assert elapsed < 1.5, elapsed
    assert app.state.mailer.drain(15)
    audit = _email_audit(repo)
    assert [a["event"] for a in audit] == ["email.failed", "email.failed"]
    for a in audit:
        assert a["context"]["attempts"] == 3
        assert a["context"]["error"] == "ConnectionRefusedError"
    tokens = _db_tokens(repo)
    assert len(tokens) == 2
    logged = "\n".join(rec.getMessage() + repr(rec.__dict__) for rec in caplog.records)
    assert "email delivery failed" in logged
    for t in tokens:
        assert t not in logged and t not in json.dumps(audit)


def test_transient_421_is_retried_then_delivered(make_app):
    with FakeSMTPServer(fail_greeting_times=2) as srv:
        app, repo = make_app(**_smtp_env(srv))
        assert _signup(TestClient(app)).status_code == 200
        assert app.state.mailer.drain(10)
        assert srv.connections == 3 and len(srv.messages) == 1
        (a,) = _email_audit(repo)
        assert a["event"] == "email.sent" and a["context"]["attempts"] == 3


def test_permanent_550_is_not_retried(make_app):
    with FakeSMTPServer(rcpt_code=550) as srv:
        app, repo = make_app(**_smtp_env(srv))
        assert _signup(TestClient(app)).status_code == 200
        assert app.state.mailer.drain(10)
        assert srv.connections == 1 and srv.messages == []
        (a,) = _email_audit(repo)
        assert a["event"] == "email.failed"
        assert a["context"]["smtp_code"] == 550
        assert a["context"]["attempts"] == 1


def test_bad_smtp_password_is_not_retried(make_app):
    with FakeSMTPServer(auth=("mailer", "right")) as srv:
        app, repo = make_app(**_smtp_env(srv, SMTP_USER="mailer",
                                         SMTP_PASSWORD="wrong"))
        assert _signup(TestClient(app)).status_code == 200
        assert app.state.mailer.drain(10)
        assert srv.connections == 1 and srv.messages == []
        (a,) = _email_audit(repo)
        assert a["context"]["error"] == "SMTPAuthenticationError"
        assert a["context"]["smtp_code"] == 535


def test_backoff_schedule_and_attempt_cap():
    from core.mailer import Mailer, SMTPConfig, SMTPTransport
    cfg = SMTPConfig(host="127.0.0.1", port=closed_port(),
                     sender="no-reply@example.test",
                     envelope_from="no-reply@example.test", tls="none",
                     timeout=2)
    slept: list[float] = []
    m = Mailer(transport=SMTPTransport(cfg), public_base_url=BASE,
               max_attempts=3, backoff_sec=1.5, sleep=slept.append)
    events: list[tuple[str, dict]] = []
    m.send_password_reset(to="bob@example.com", token="tok" * 10,
                          audit=lambda e, ctx: events.append((e, ctx)))
    assert m.drain(10)
    assert slept == [1.5, 3.0]
    assert events == [("email.failed", {
        "kind": "reset", "transport": "smtp", "attempts": 3,
        "error": "ConnectionRefusedError"})]


# ── no SMTP configured ───────────────────────────────────────────


@pytest.mark.parametrize("env", ["production", "staging"])
def test_deployed_without_smtp_leaks_no_token_anywhere(make_app, caplog,
                                                        capfd, env):
    caplog.set_level(logging.DEBUG)
    app, repo = make_app(env=env, VIDEO_FACTORY_PUBLIC_BASE_URL=BASE,
                         VIDEO_FACTORY_DEV_EXPOSE_TOKENS="1")
    assert app.state.mailer.status() == {
        "email": "not_configured", "email_transport": "null"}
    c = TestClient(app)
    responses = [
        _signup(c, headers=ORIGIN),
        c.post("/auth/forgot", json={"email": "alice@example.com"}, headers=ORIGIN),
        c.post("/auth/forgot", json={"email": "nobody@example.com"}, headers=ORIGIN),
    ]
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert responses[1].json() == responses[2].json() == {"ok": True}
    assert app.state.mailer.drain(5)
    tokens = _db_tokens(repo)
    assert len(tokens) == 2
    out, err = capfd.readouterr()
    logged = "\n".join(
        rec.getMessage() + repr(rec.__dict__) for rec in caplog.records)
    assert "email not configured" in logged
    audit_rows = json.dumps(repo._conn().execute(
        "SELECT event, context_json FROM audit_log").fetchall())
    haystacks = [out, err, logged, audit_rows] + [r.text for r in responses] \
        + [json.dumps(dict(r.headers)) for r in responses]
    for t in tokens:
        for h in haystacks:
            assert t not in h
    assert _email_audit(repo) == []


def test_deployed_with_smtp_but_no_public_base_url_does_not_send(make_app):
    with FakeSMTPServer() as srv:
        env = _smtp_env(srv)
        env.pop("VIDEO_FACTORY_PUBLIC_BASE_URL")
        app, repo = make_app(env="production", **env)
        st = app.state.mailer.status()
        assert st["email"] == "not_configured"
        assert st["email_error"] == "missing_public_base_url"
        r = _signup(TestClient(app), headers={"Origin": "http://testserver"})
        assert r.status_code == 200
        assert app.state.mailer.drain(5)
        assert srv.connections == 0


def test_dev_console_transport_redacts_token(make_app, capfd):
    app, repo = make_app(env="dev")
    assert app.state.mailer.status() == {
        "email": "not_configured", "email_transport": "console"}
    assert _signup(TestClient(app)).status_code == 200
    _, err = capfd.readouterr()
    (token,) = _db_tokens(repo)
    assert "[video-factory email/console] kind=verify" in err
    assert "to=a***@example.com" in err
    assert "http://localhost:5173/verify/<redacted>" in err
    assert token not in err
    (a,) = _email_audit(repo)
    assert a["event"] == "email.sent" and a["context"]["transport"] == "console"


def test_dev_console_full_link_only_with_explicit_opt_in(make_app, capfd):
    app, repo = make_app(env="dev", VIDEO_FACTORY_DEV_EXPOSE_TOKENS="1")
    assert _signup(TestClient(app)).status_code == 200
    _, err = capfd.readouterr()
    (token,) = _db_tokens(repo)
    assert f"http://localhost:5173/verify/{token}" in err


def test_unset_env_without_smtp_is_null_not_console(make_app, capfd):
    app, repo = make_app(env=None)
    assert app.state.mailer.status()["email_transport"] == "null"
    assert _signup(TestClient(app)).status_code == 200
    _, err = capfd.readouterr()
    assert "email/console" not in err


# ── config parsing ───────────────────────────────────────────────


def _status(monkeypatch, **env):
    for k in _ENV_TO_CLEAR:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    from core.mailer import Mailer
    return Mailer.from_env()


@pytest.mark.parametrize("tls,port", [("starttls", 587), ("ssl", 465),
                                      ("none", 25), ("", 587)])
def test_default_ports(monkeypatch, tls, port):
    m = _status(monkeypatch, SMTP_HOST="smtp.example.test", SMTP_TLS=tls,
                SMTP_FROM="no-reply@example.test")
    assert m.transport.name == "smtp" and m.transport.cfg.port == port


@pytest.mark.parametrize("env,code", [
    ({"SMTP_TLS": "maybe"}, "invalid_smtp_tls"),
    ({"SMTP_PORT": "http"}, "invalid_smtp_port"),
    ({"SMTP_PORT": "70000"}, "invalid_smtp_port"),
    ({"SMTP_FROM": ""}, "missing_smtp_from"),
    ({"SMTP_FROM": "not-an-address"}, "invalid_smtp_from"),
    ({"SMTP_FROM": "a@b.test, c@d.test"}, "invalid_smtp_from"),
    ({"SMTP_PASSWORD": "pw"}, "smtp_password_without_user"),
    ({"SMTP_TIMEOUT_SEC": "0"}, "invalid_smtp_timeout"),
    ({"SMTP_CA_FILE": "/nonexistent/ca.pem"}, "invalid_smtp_ca_file"),
    ({"SMTP_HOST": "smtp.example.test/x"}, "invalid_smtp_host"),
])
def test_invalid_config_is_not_configured_with_reason(monkeypatch, env, code):
    base = {"SMTP_HOST": "smtp.example.test", "SMTP_FROM": "no-reply@example.test",
            "VIDEO_FACTORY_ENV": "production",
            "VIDEO_FACTORY_PUBLIC_BASE_URL": BASE}
    base.update(env)
    m = _status(monkeypatch, **base)
    assert m.status() == {"email": "not_configured",
                          "email_transport": "null", "email_error": code}


def test_plaintext_auth_refused_in_production_allowed_in_dev(monkeypatch):
    env = {"SMTP_HOST": "smtp.example.test", "SMTP_FROM": "no-reply@example.test",
           "SMTP_TLS": "none", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
           "VIDEO_FACTORY_PUBLIC_BASE_URL": BASE}
    m = _status(monkeypatch, VIDEO_FACTORY_ENV="production", **env)
    assert m.status()["email_error"] == "plaintext_auth_refused"
    m = _status(monkeypatch, VIDEO_FACTORY_ENV="dev", **env)
    assert m.status()["email"] == "configured"


def test_smtp_password_never_in_repr_or_status(monkeypatch):
    m = _status(monkeypatch, SMTP_HOST="smtp.example.test",
                SMTP_FROM="no-reply@example.test", SMTP_USER="u",
                SMTP_PASSWORD="sup3r-secret-pw")
    assert "sup3r-secret-pw" not in repr(m.transport.cfg)
    assert "sup3r-secret-pw" not in json.dumps(m.status())


@pytest.mark.parametrize("bad", [
    "victim@example.com\r\nBcc: attacker@evil.test",
    "victim@example.com\nBcc: attacker@evil.test",
    "local,attacker@evil.test",
    "<victim@example.com>",
    '"a b"@example.com',
    "victim@example.com attacker@evil.test",
    "no-at-sign",
])
def test_recipient_header_injection_refused_before_connecting(bad):
    from core.mailer import Mailer, SMTPConfig, SMTPTransport
    with FakeSMTPServer() as srv:
        cfg = SMTPConfig(host="127.0.0.1", port=srv.port,
                         sender="no-reply@example.test",
                         envelope_from="no-reply@example.test", tls="none")
        m = Mailer(transport=SMTPTransport(cfg), public_base_url=BASE)
        events: list = []
        m.send_verification(to=bad, token="t" * 32,
                            audit=lambda e, ctx: events.append((e, ctx)))
        assert m.drain(5)
        assert srv.connections == 0
        assert events == [("email.failed", {
            "kind": "verify", "transport": "smtp", "attempts": 0,
            "reason": "invalid_recipient"})]


def test_public_base_url_trailing_slash_and_template_escaping():
    from core.email_templates import render_password_reset, reset_link, verify_link
    assert verify_link("https://x.test/", "abc") == "https://x.test/verify/abc"
    assert reset_link("https://x.test/app/", "a/b+c") == \
        "https://x.test/app/reset?token=a%2Fb%2Bc"
    r = render_password_reset(link='https://x.test/reset?token=a"<b>&c')
    assert '"<b>' not in r.html and "&quot;&lt;b&gt;&amp;c" in r.html


# ── /readyz info ─────────────────────────────────────────────────


def test_readyz_reports_email_configured_as_info(make_app):
    with FakeSMTPServer() as srv:
        app, _ = make_app(**_smtp_env(srv))
        r = TestClient(app).get("/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["info"] == {"email": "configured", "email_transport": "smtp"}
        assert "email" not in body["checks"] and "email" not in body["errors"]


def test_readyz_not_configured_is_not_a_failure(make_app):
    app, _ = make_app(env=None)
    r = TestClient(app).get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready"
    assert r.json()["info"] == {"email": "not_configured", "email_transport": "null"}


def test_readyz_invalid_smtp_config_reports_reason_without_values(make_app):
    app, _ = make_app(env=None, SMTP_HOST="smtp.example.test",
                      SMTP_FROM="no-reply@example.test", SMTP_TLS="weird",
                      SMTP_USER="mailer-user", SMTP_PASSWORD="pw-value-x")
    r = TestClient(app).get("/readyz")
    assert r.status_code == 200
    assert r.json()["info"]["email_error"] == "invalid_smtp_tls"
    for v in ("smtp.example.test", "mailer-user", "pw-value-x", "weird"):
        assert v not in r.text


# ── web: reset page ──────────────────────────────────────────────


def test_reset_page_is_public_and_wired_to_consume():
    server = (REPO_ROOT / "web/src/routes/reset/+page.server.ts").read_text()
    page = (REPO_ROOT / "web/src/routes/reset/+page.svelte").read_text()
    allow = (REPO_ROOT / "web/src/lib/server/auth-allowlist.ts").read_text()
    assert '"/reset"' in allow
    assert 'from "$lib/server/upstream"' in server
    i = server.index('"/auth/forgot/consume"')
    assert "...upstream_headers()" in server[i:i + 300]
    assert 'name="new_password"' in page and 'type="password"' in page
    # The token in the URL must not leak through Referer to another
    # origin. Not "no-referrer": Chromium then sends `Origin: null` on
    # the same-origin form POST and the S1b Origin check refuses it
    # (found by the live browser walk).
    assert 'name="referrer" content="same-origin"' in page
    assert 'content="no-referrer"' not in page
    verify_page = (REPO_ROOT / "web/src/routes/verify/[token]/+page.svelte").read_text()
    assert 'name="referrer" content="same-origin"' in verify_page
    # The root layout must render /reset and /verify/<token> as bare
    # auth cards: a visitor from an email has no workspace, and the
    # shell would otherwise show the onboarding screen instead of the
    # page (live walk found this).
    layout = (REPO_ROOT / "web/src/routes/+layout.svelte").read_text()
    m = re.search(r"const PUBLIC_AUTH = \[([^\]]*)\]", layout)
    assert m and "'/reset'" in m.group(1)
    assert "startsWith('/verify/')" in layout
