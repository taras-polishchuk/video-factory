"""SECURITY S1b regression tests (t_ca20feb0).

One block per finding. Every block has at least one test that FAILS on
main 457f8f4 and passes with the fix (new modules are imported inside
the tests, so on the old tree those tests fail one by one instead of
breaking collection).

  F8   session fixation: login / signup / OIDC sign-in rotate the id
  F9   require_csrf refuses expired, revoked and locked sessions
  F5/F7 Origin / Referer allow-list on pre-session /auth/* routes
  F26  per-IP / per-email rate limits + trusted-proxy client IP
  F11/F15 admin key rotation: CSRF + role + re-auth, no key material
  F12  HeyGen webhook replay dedup
  F14  CORS only from an explicit allow-list
  F19  hardening headers on every API response
  F24/F25 PKCE S256 on OIDC, GitHub scope user:email only
  F29  session secret rotation (verify-only previous secret)
  A2 F-05 Secure cookie on HTTPS / production / staging
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET = "s1b-session-secret-" + "a" * 40
PASSWORD = "Password12345!"

_ENV_TO_CLEAR = (
    "VIDEO_FACTORY_ENV", "VF_HTTPS", "VIDEO_FACTORY_REQUIRE_ORIGIN",
    "VIDEO_FACTORY_PUBLIC_BASE_URL", "VIDEO_FACTORY_CORS_ORIGINS",
    "VIDEO_FACTORY_TRUSTED_PROXIES", "VIDEO_FACTORY_SESSION_SECRET_PREVIOUS",
    "VIDEO_FACTORY_DEV_EXPOSE_TOKENS", "HEYGEN_WEBHOOK_SECRET",
    "VF_SIGNUP_IP_LIMIT", "VF_SIGNUP_IP_WINDOW_SEC",
    "VF_FORGOT_IP_LIMIT", "VF_FORGOT_IP_WINDOW_SEC",
    "VF_FORGOT_EMAIL_LIMIT", "VF_FORGOT_EMAIL_WINDOW_SEC",
    "VF_RESET_CONSUME_IP_LIMIT", "VF_RESET_CONSUME_IP_WINDOW_SEC",
    "VF_VERIFY_IP_LIMIT", "VF_VERIFY_IP_WINDOW_SEC",
    "VF_KEYS_REAUTH_LIMIT", "VF_KEYS_REAUTH_WINDOW_SEC",
    "VIDEO_FACTORY_DB_KEY", "SENTRY_DSN",
    "GOOGLE_OIDC_CLIENT_ID", "GOOGLE_OIDC_CLIENT_SECRET",
    "GITHUB_OIDC_CLIENT_ID", "GITHUB_OIDC_CLIENT_SECRET",
)


@pytest.fixture()
def make_app(tmp_path, monkeypatch):
    for k in _ENV_TO_CLEAR:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", SECRET)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(tmp_path / "storage"))

    def _make(*, db_key=None):
        from api.main import create_app
        from core.repo import Repo
        from core.runners import JobRunner, Singleton
        repo = Repo(str(tmp_path / "vf.db"), db_key=db_key) if db_key \
            else Repo(str(tmp_path / "vf.db"))
        runner = JobRunner(
            repo=repo, storage_root=str(tmp_path / "storage"), schema={},
            budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
        )
        Singleton.set(runner)
        app = create_app(repo=repo, runner=runner,
                         storage_root=str(tmp_path / "storage"), schema={})
        return app, repo
    return _make


def _signup(client, email="alice@example.com", headers=None):
    return client.post("/auth/signup", json={
        "email": email, "password": PASSWORD, "workspace_name": "Acme",
    }, headers=headers or {})


def _csrf(sid: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), sid.encode(), hashlib.sha256).hexdigest()


def _cookie(sid: str) -> dict:
    return {"Cookie": f"vf_session={sid}"}


def _future_iso(seconds=3600):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def _plant_session(repo, user_id, sid=None, expires_at=None):
    sid = sid or "planted-" + secrets.token_hex(12)
    repo.create_session(user_id=user_id, csrf_token="x", ip="198.51.100.66",
                        user_agent="attacker", expires_at=expires_at or _future_iso(),
                        session_id=sid)
    return sid


# ════════════════════════════════════════════════════════════════════
# F8  session fixation
# ════════════════════════════════════════════════════════════════════

def test_f8_login_deletes_planted_session_and_issues_new_id(make_app):
    app, repo = make_app()
    uid = _signup(TestClient(app)).json()["user_id"]
    planted = _plant_session(repo, uid)
    victim = TestClient(app)
    r = victim.post("/auth/login", json={"email": "alice@example.com",
                                          "password": PASSWORD},
                    headers=_cookie(planted))
    assert r.status_code == 200, r.text
    new_sid = r.cookies.get("vf_session")
    assert new_sid and new_sid != planted
    assert repo.get_session(planted) is None, "planted session survived login"
    attacker = TestClient(app)
    assert attacker.get("/auth/me", headers=_cookie(planted)).status_code == 401
    assert attacker.get("/auth/me", headers=_cookie(new_sid)).status_code == 200


def test_f8_failed_login_keeps_existing_session(make_app):
    """Rotation happens only on SUCCESS: a typo must not log you out."""
    app, repo = make_app()
    uid = _signup(TestClient(app)).json()["user_id"]
    live = _plant_session(repo, uid)
    r = TestClient(app).post("/auth/login", json={"email": "alice@example.com",
                                                  "password": "wrong-Password1"},
                             headers=_cookie(live))
    assert r.status_code == 401
    assert repo.get_session(live) is not None


def test_f8_signup_deletes_presented_session(make_app):
    app, repo = make_app()
    uid = _signup(TestClient(app), email="first@example.com").json()["user_id"]
    planted = _plant_session(repo, uid)
    r = _signup(TestClient(app), email="second@example.com",
                headers=_cookie(planted))
    assert r.status_code == 200, r.text
    assert r.cookies.get("vf_session") != planted
    assert repo.get_session(planted) is None


# ════════════════════════════════════════════════════════════════════
# F9  require_csrf must enforce a LIVE session
# ════════════════════════════════════════════════════════════════════

def _app_with_csrf_probe(make_app):
    from fastapi import Depends
    from auth.dependencies import require_csrf
    app, repo = make_app()

    @app.post("/_s1b/csrf-probe", dependencies=[Depends(require_csrf)])
    async def _probe():
        return {"ok": True}
    return app, repo


def test_f9_require_csrf_rejects_expired_session(make_app):
    app, repo = _app_with_csrf_probe(make_app)
    uid = _signup(TestClient(app)).json()["user_id"]
    sid = _plant_session(repo, uid, expires_at="2001-01-01T00:00:00Z")
    r = TestClient(app).post("/_s1b/csrf-probe",
                             headers={**_cookie(sid), "X-CSRF-Token": _csrf(sid)})
    assert r.status_code == 401, r.text
    assert "expired" in r.text
    assert repo.get_session(sid) is None, "expired row should be purged"


def test_f9_require_csrf_rejects_revoked_and_locked(make_app):
    app, repo = _app_with_csrf_probe(make_app)
    uid = _signup(TestClient(app)).json()["user_id"]
    c = TestClient(app)
    live = _plant_session(repo, uid)
    ok = c.post("/_s1b/csrf-probe", headers={**_cookie(live), "X-CSRF-Token": _csrf(live)})
    assert ok.status_code == 200, ok.text
    repo.delete_session(live)
    revoked = c.post("/_s1b/csrf-probe", headers={**_cookie(live), "X-CSRF-Token": _csrf(live)})
    assert revoked.status_code == 401
    live2 = _plant_session(repo, uid)
    repo._conn().execute("UPDATE users SET status='locked' WHERE user_id=?", (uid,))
    repo._conn().commit()
    locked = c.post("/_s1b/csrf-probe", headers={**_cookie(live2), "X-CSRF-Token": _csrf(live2)})
    assert locked.status_code == 423


# ════════════════════════════════════════════════════════════════════
# F5 / F7  Origin / Referer allow-list on pre-session routes
# ════════════════════════════════════════════════════════════════════

_PRE_SESSION = [
    ("/auth/signup", {"email": "x1@example.com", "password": PASSWORD,
                      "workspace_name": "X"}),
    ("/auth/login", {"email": "x1@example.com", "password": PASSWORD}),
    ("/auth/forgot", {"email": "x1@example.com"}),
    ("/auth/forgot/consume", {"token": "nope", "new_password": PASSWORD}),
    ("/auth/verify", {"token": "nope"}),
]


@pytest.mark.parametrize("path,body", _PRE_SESSION)
def test_f5_f7_cross_site_origin_refused(make_app, path, body):
    app, _ = make_app()
    r = TestClient(app).post(path, json=body,
                             headers={"Origin": "https://evil.example"})
    assert r.status_code == 403, (path, r.status_code, r.text)


@pytest.mark.parametrize("path,body", _PRE_SESSION)
def test_f5_f7_null_origin_and_foreign_referer_refused(make_app, path, body):
    app, _ = make_app()
    c = TestClient(app)
    assert c.post(path, json=body, headers={"Origin": "null"}).status_code == 403
    r = c.post(path, json=body, headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 403, (path, r.text)


def test_f5_allow_listed_and_same_origin_accepted(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_PUBLIC_BASE_URL", "https://app.example.com/")
    app, _ = make_app()
    c = TestClient(app)
    r = _signup(c, email="a1@example.com", headers={"Origin": "https://app.example.com"})
    assert r.status_code == 200, r.text
    r = _signup(c, email="a2@example.com",
                headers={"Referer": "https://app.example.com/signup?x=1"})
    assert r.status_code == 200, r.text
    r = _signup(c, email="a3@example.com", headers={"Origin": "http://testserver"})
    assert r.status_code == 200, r.text
    # A look-alike host is not the allow-listed origin.
    r = _signup(c, email="a4@example.com",
                headers={"Origin": "https://app.example.com.evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("env", ["production", "staging"])
def test_f5_strict_mode_signup_without_origin_refused(make_app, monkeypatch, env):
    monkeypatch.setenv("VIDEO_FACTORY_ENV", env)
    app, _ = make_app()
    r = _signup(TestClient(app), email=f"{env}@example.com")
    assert r.status_code == 403, r.text
    assert "origin_missing" in r.text


def test_f5_internal_token_admits_sveltekit_server(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_ENV", "production")
    from auth.session import INTERNAL_TOKEN_HEADER, derive_internal_token
    app, _ = make_app()
    c = TestClient(app)
    good = {INTERNAL_TOKEN_HEADER: derive_internal_token()}
    assert _signup(c, email="sk1@example.com", headers=good).status_code == 200
    bad = {INTERNAL_TOKEN_HEADER: "0" * 64}
    r = _signup(c, email="sk2@example.com", headers=bad)
    assert r.status_code == 403 and "internal_token_invalid" in r.text
    # A wrong token is refused even when a valid Origin is also sent.
    r = _signup(c, email="sk3@example.com",
                headers={**bad, "Origin": "http://testserver"})
    assert r.status_code == 403


def test_f5_dev_mode_allows_non_browser_calls_without_origin(make_app):
    """curl / server-to-server with no Origin and no Referer: allowed
    outside production (browsers always send Origin on POST)."""
    app, _ = make_app()
    assert _signup(TestClient(app)).status_code == 200


def test_f5_sveltekit_actions_send_internal_token_and_xff():
    upstream = (REPO_ROOT / "web/src/lib/server/upstream.ts").read_text()
    assert '"x-vf-internal-token"' in upstream
    assert '"x-forwarded-for"' in upstream
    assert "getClientAddress" in upstream
    # A cross-site browser POST must not be laundered into a trusted
    # server-side call: no token, browser Origin forwarded instead.
    assert "cross_site_origin" in upstream and 'h["origin"] = foreign' in upstream
    for rel, ep in [
        ("web/src/routes/signup/+page.server.ts", "/auth/signup"),
        ("web/src/routes/login/+page.server.ts", "/auth/login"),
        ("web/src/routes/forgot/+page.server.ts", "/auth/forgot"),
        ("web/src/routes/verify/[token]/+page.server.ts", "/auth/verify"),
    ]:
        src = (REPO_ROOT / rel).read_text()
        assert 'from "$lib/server/upstream"' in src, rel
        i = src.index(f'"{ep}"')
        assert "...upstream_headers()" in src[i:i + 300], rel


def test_f5_internal_token_parity_python_vs_node(monkeypatch):
    """upstream.ts must derive exactly what auth/session.py verifies."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", SECRET)
    upstream = (REPO_ROOT / "web/src/lib/server/upstream.ts").read_text()
    label = re.search(r'\.update\("([^"]+)", "utf-8"\)', upstream).group(1)
    js = ("const c=require('crypto');process.stdout.write("
          "c.createHmac('sha256',process.argv[1]).update(process.argv[2],'utf-8')"
          ".digest('hex'))")
    out = subprocess.run([node, "-e", js, SECRET, label], capture_output=True,
                         text=True, timeout=30, check=True).stdout
    from auth.session import derive_internal_token, verify_internal_token
    assert out == derive_internal_token()
    assert verify_internal_token(out)


# ════════════════════════════════════════════════════════════════════
# F26  rate limits + trusted-proxy client IP
# ════════════════════════════════════════════════════════════════════

def test_f26_signup_rate_limited_per_ip(make_app, monkeypatch):
    monkeypatch.setenv("VF_SIGNUP_IP_LIMIT", "2")
    app, _ = make_app()
    c = TestClient(app)
    assert _signup(c, email="r1@example.com").status_code == 200
    assert _signup(c, email="r2@example.com").status_code == 200
    r = _signup(c, email="r3@example.com")
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) > 0


def test_f26_forgot_rate_limited_per_ip_and_per_email(make_app, monkeypatch):
    monkeypatch.setenv("VF_FORGOT_IP_LIMIT", "3")
    app, _ = make_app()
    c = TestClient(app)
    for i in range(3):
        assert c.post("/auth/forgot", json={"email": f"f{i}@example.com"}).status_code == 200
    assert c.post("/auth/forgot", json={"email": "f9@example.com"}).status_code == 429

    monkeypatch.setenv("VF_FORGOT_IP_LIMIT", "100")
    monkeypatch.setenv("VF_FORGOT_EMAIL_LIMIT", "2")
    app2, _ = make_app()
    c2 = TestClient(app2, client=("203.0.113.10", 1000))
    c3 = TestClient(app2, client=("203.0.113.11", 1000))
    assert c2.post("/auth/forgot", json={"email": "Victim@example.com"}).status_code == 200
    assert c3.post("/auth/forgot", json={"email": "victim@example.com"}).status_code == 200
    # Third request for the same address from yet another IP: 429.
    r = TestClient(app2, client=("203.0.113.12", 1000)).post(
        "/auth/forgot", json={"email": "victim@example.com"})
    assert r.status_code == 429
    # Other addresses are unaffected.
    assert c2.post("/auth/forgot", json={"email": "other@example.com"}).status_code == 200


def test_f26_forgot_email_counter_does_not_store_address(make_app, monkeypatch):
    monkeypatch.setenv("VF_FORGOT_EMAIL_LIMIT", "5")
    app, repo = make_app()
    TestClient(app).post("/auth/forgot", json={"email": "private@example.com"})
    rows = repo._conn().execute("SELECT bucket, key FROM rate_limit_hits").fetchall()
    assert rows
    assert all("private@example.com" not in k for _, k in rows)


@pytest.mark.parametrize("path,env_key,body", [
    ("/auth/forgot/consume", "VF_RESET_CONSUME_IP_LIMIT",
     {"token": "guess", "new_password": PASSWORD}),
    ("/auth/verify", "VF_VERIFY_IP_LIMIT", {"token": "guess"}),
])
def test_f26_token_endpoints_rate_limited_per_ip(make_app, monkeypatch, path, env_key, body):
    monkeypatch.setenv(env_key, "2")
    app, _ = make_app()
    c = TestClient(app)
    assert c.post(path, json=body).status_code == 404
    assert c.post(path, json=body).status_code == 404
    assert c.post(path, json=body).status_code == 429


def test_f26_xff_from_trusted_proxy_separates_clients(make_app, monkeypatch):
    monkeypatch.setenv("VF_SIGNUP_IP_LIMIT", "1")
    app, _ = make_app()
    via_web = TestClient(app, client=("127.0.0.1", 40000))   # SvelteKit on loopback
    h1 = {"X-Forwarded-For": "203.0.113.5, 127.0.0.1"}
    h2 = {"X-Forwarded-For": "203.0.113.6, 127.0.0.1"}
    assert _signup(via_web, email="p1@example.com", headers=h1).status_code == 200
    assert _signup(via_web, email="p2@example.com", headers=h1).status_code == 429
    assert _signup(via_web, email="p3@example.com", headers=h2).status_code == 200


def test_f26_xff_from_untrusted_peer_is_ignored(make_app, monkeypatch):
    monkeypatch.setenv("VF_SIGNUP_IP_LIMIT", "1")
    app, _ = make_app()
    direct = TestClient(app, client=("198.51.100.7", 40000))
    assert _signup(direct, email="s1@example.com",
                   headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 200
    r = _signup(direct, email="s2@example.com",
                headers={"X-Forwarded-For": "203.0.113.2"})
    assert r.status_code == 429, "spoofed XFF from an untrusted peer opened a new bucket"


def _req(peer, xff=None, proto=None):
    from starlette.requests import Request
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if proto is not None:
        headers.append((b"x-forwarded-proto", proto.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers,
                    "client": (peer, 1), "scheme": "http", "query_string": b"",
                    "server": ("testserver", 80)})


def test_f26_client_ip_rightmost_untrusted_rule(monkeypatch):
    monkeypatch.delenv("VIDEO_FACTORY_TRUSTED_PROXIES", raising=False)
    from auth.client_ip import client_ip
    assert client_ip(_req("198.51.100.7", "1.2.3.4")) == "198.51.100.7"
    assert client_ip(_req("127.0.0.1", "1.2.3.4")) == "1.2.3.4"
    # Caddy (docker bridge) -> SvelteKit (docker bridge) -> API
    assert client_ip(_req("172.18.0.3", "203.0.113.9, 172.18.0.2")) == "203.0.113.9"
    # Client-supplied junk on the left never wins.
    assert client_ip(_req("127.0.0.1", "6.6.6.6, 203.0.113.9")) == "203.0.113.9"
    # Every hop trusted: leftmost.
    assert client_ip(_req("127.0.0.1", "127.0.0.1, 172.18.0.2")) == "127.0.0.1"
    assert client_ip(_req("127.0.0.1", "not-an-ip")) == "127.0.0.1"
    monkeypatch.setenv("VIDEO_FACTORY_TRUSTED_PROXIES", "")
    assert client_ip(_req("127.0.0.1", "1.2.3.4")) == "127.0.0.1"
    monkeypatch.setenv("VIDEO_FACTORY_TRUSTED_PROXIES", "10.9.0.0/16")
    assert client_ip(_req("10.9.1.1", "1.2.3.4")) == "1.2.3.4"
    assert client_ip(_req("127.0.0.1", "1.2.3.4")) == "127.0.0.1"


def test_f26_login_attempts_record_real_client_ip(make_app):
    app, repo = make_app()
    _signup(TestClient(app))
    via_web = TestClient(app, client=("127.0.0.1", 40000))
    via_web.post("/auth/login", json={"email": "alice@example.com", "password": "bad-Password1"},
                 headers={"X-Forwarded-For": "203.0.113.77"})
    ips = {r[0] for r in repo._conn().execute("SELECT ip FROM login_attempts").fetchall()}
    assert ips == {"203.0.113.77"}


# ════════════════════════════════════════════════════════════════════
# F11 / F15  POST /api/v1/admin/keys/rotate
# ════════════════════════════════════════════════════════════════════

OLD_KEY = "s1b-old-db-key-" + "Q" * 24
NEW_KEY = "s1b-new-db-key-" + "Z" * 24


def _owner(app, email="owner@example.com"):
    c = TestClient(app)
    r = _signup(c, email=email)
    assert r.status_code == 200, r.text
    sid = r.cookies.get("vf_session")
    return c, r.json()["user_id"], sid


def _rotate(app, sid, body, csrf=True):
    headers = _cookie(sid)
    if csrf:
        headers["X-CSRF-Token"] = _csrf(sid)
    return TestClient(app).post("/api/v1/admin/keys/rotate", headers=headers,
                                content=json.dumps(body))


def test_f11_rotate_without_csrf_refused(make_app):
    app, _ = make_app(db_key=OLD_KEY)
    _, _, sid = _owner(app)
    r = _rotate(app, sid, {"old_key": OLD_KEY, "new_key": NEW_KEY,
                           "current_password": PASSWORD}, csrf=False)
    assert r.status_code == 403, r.text


def test_f11_rotate_without_or_with_wrong_password_refused(make_app):
    app, repo = make_app(db_key=OLD_KEY)
    _, uid, sid = _owner(app)
    r = _rotate(app, sid, {"old_key": OLD_KEY, "new_key": NEW_KEY})
    assert r.status_code == 403, r.text
    r = _rotate(app, sid, {"old_key": OLD_KEY, "new_key": NEW_KEY,
                           "current_password": "Wrong-password-1"})
    assert r.status_code == 403, r.text
    ev = [row[0] for row in repo._conn().execute(
        "SELECT event FROM audit_log WHERE user_id=?", (uid,)).fetchall()]
    assert "admin.keys_rotate_reauth_failed" in ev
    # The DB is still on the old key (no rotation happened).
    assert repo.db_key == OLD_KEY


def test_f11_rotate_requires_owner_or_admin_role(make_app):
    app, repo = make_app(db_key=OLD_KEY)
    _, _, _ = _owner(app, email="boss@example.com")
    c = TestClient(app)
    r = _signup(c, email="editor@example.com")
    uid = r.json()["user_id"]
    sid = r.cookies.get("vf_session")
    # Demote: the only membership left is "editor".
    repo._conn().execute("UPDATE memberships SET role='editor' WHERE user_id=?", (uid,))
    repo._conn().commit()
    r = _rotate(app, sid, {"old_key": OLD_KEY, "new_key": NEW_KEY,
                           "current_password": PASSWORD})
    assert r.status_code == 403
    assert "owner or admin" in r.text


def test_f11_reauth_attempts_are_budgeted(make_app, monkeypatch):
    monkeypatch.setenv("VF_KEYS_REAUTH_LIMIT", "2")
    app, _ = make_app(db_key=OLD_KEY)
    _, _, sid = _owner(app)
    bad = {"old_key": OLD_KEY, "new_key": NEW_KEY, "current_password": "Nope-nope-123"}
    assert _rotate(app, sid, bad).status_code == 403
    assert _rotate(app, sid, bad).status_code == 403
    assert _rotate(app, sid, bad).status_code == 429


def test_f15_malformed_body_is_not_echoed(make_app):
    app, _ = make_app(db_key=OLD_KEY)
    _, _, sid = _owner(app)
    r = _rotate(app, sid, [OLD_KEY, NEW_KEY])
    assert r.status_code == 400, r.text
    assert OLD_KEY not in r.text and NEW_KEY not in r.text


def _all_audit_text(repo) -> str:
    parts = []
    for table in ("audit_log", "runner_audit"):
        try:
            parts += [json.dumps(list(r)) for r in
                      repo._conn().execute(f"SELECT * FROM {table}").fetchall()]
        except Exception:  # noqa: BLE001 - table may not exist
            pass
    return "\n".join(parts)


def test_f15_successful_rotation_logs_fingerprints_only(make_app, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    app, repo = make_app(db_key=OLD_KEY)
    _, _, sid = _owner(app)
    r = _rotate(app, sid, {"old_key": OLD_KEY, "new_key": NEW_KEY,
                           "current_password": PASSWORD})
    assert r.status_code == 200, r.text
    assert repo.db_key == NEW_KEY
    fp_new = hashlib.sha256(NEW_KEY.encode()).hexdigest()[:16]
    assert fp_new in r.text or fp_new in _all_audit_text(repo)
    out = capsys.readouterr()
    blobs = [r.text, caplog.text, out.out, out.err, _all_audit_text(repo)]
    for blob in blobs:
        assert OLD_KEY not in blob and NEW_KEY not in blob
    assert any(getattr(rec, "new_key_fingerprint", None) == fp_new
               for rec in caplog.records), "rotation request not logged by fingerprint"


def test_f15_failed_rotation_leaks_no_key_material(make_app, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    app, repo = make_app(db_key=OLD_KEY)
    _, _, sid = _owner(app)
    wrong_old = "s1b-not-the-real-old-key-" + "W" * 20
    r = _rotate(app, sid, {"old_key": wrong_old, "new_key": NEW_KEY,
                           "current_password": PASSWORD})
    assert r.status_code in (400, 500, 503), r.text
    out = capsys.readouterr()
    for blob in (r.text, caplog.text, out.out, out.err, _all_audit_text(repo)):
        for k in (wrong_old, NEW_KEY, OLD_KEY):
            assert k not in blob


def test_f15_redact_keys_helper():
    from control_plane.key_rotation import redact_keys
    msg = redact_keys(f"PRAGMA key='{OLD_KEY}' failed for {NEW_KEY}", OLD_KEY, NEW_KEY)
    assert OLD_KEY not in msg and NEW_KEY not in msg
    assert "[key:" in msg


# ════════════════════════════════════════════════════════════════════
# F12  HeyGen webhook replay
# ════════════════════════════════════════════════════════════════════

def _signed(body: bytes, secret="hg-secret"):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_f12_webhook_replay_is_acknowledged_but_not_processed(make_app, monkeypatch):
    monkeypatch.setenv("HEYGEN_WEBHOOK_SECRET", "hg-secret")
    app, repo = make_app()
    calls = []
    monkeypatch.setattr(repo, "list_jobs", lambda **kw: [
        {"job_id": "job_s1b", "provider_job_id": "v_s1b"}])
    monkeypatch.setattr(repo, "update_job", lambda job_id, **f: calls.append((job_id, f)))
    body = b'{"event_type":"video.failed","video_id":"v_s1b"}'
    h = {"X-Heygen-Signature": _signed(body)}
    c = TestClient(app)
    r1 = c.post("/api/v1/webhooks/heygen", content=body, headers=h)
    assert r1.status_code == 200 and not r1.json().get("duplicate")
    assert len(calls) == 1
    r2 = c.post("/api/v1/webhooks/heygen", content=body, headers=h)
    assert r2.status_code == 200, r2.text
    assert r2.json().get("duplicate") is True
    assert len(calls) == 1, "replayed webhook drove a state change again"


def test_f12_distinct_delivery_still_processed_and_bad_sig_refused(make_app, monkeypatch):
    monkeypatch.setenv("HEYGEN_WEBHOOK_SECRET", "hg-secret")
    app, repo = make_app()
    c = TestClient(app)
    b1 = b'{"event_type":"video.completed","video_id":"v_1"}'
    b2 = b'{"event_type":"video.completed","video_id":"v_2"}'
    assert c.post("/api/v1/webhooks/heygen", content=b1,
                  headers={"X-Heygen-Signature": _signed(b1)}).json().get("duplicate") is None
    r = c.post("/api/v1/webhooks/heygen", content=b2,
               headers={"X-Heygen-Signature": _signed(b2)})
    assert r.status_code == 200 and r.json().get("duplicate") is None
    r = c.post("/api/v1/webhooks/heygen", content=b1,
               headers={"X-Heygen-Signature": _signed(b1, "other")})
    assert r.status_code == 401
    keys = repo._conn().execute(
        "SELECT dedup_key FROM webhook_receipts WHERE source='heygen'").fetchall()
    assert {k[0] for k in keys} == {hashlib.sha256(b1).hexdigest(),
                                    hashlib.sha256(b2).hexdigest()}


# ════════════════════════════════════════════════════════════════════
# F14  CORS
# ════════════════════════════════════════════════════════════════════

def _preflight(c, origin):
    return c.options("/auth/login", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})


def test_f14_no_cors_by_default(make_app):
    app, _ = make_app()
    r = _preflight(TestClient(app), "https://evil.example")
    assert "access-control-allow-origin" not in r.headers


def test_f14_env_allow_list_is_explicit(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_CORS_ORIGINS", "https://app.example.com, *")
    app, _ = make_app()
    c = TestClient(app)
    ok = _preflight(c, "https://app.example.com")
    assert ok.headers.get("access-control-allow-origin") == "https://app.example.com"
    assert ok.headers.get("access-control-allow-origin") != "*"
    evil = _preflight(c, "https://evil.example")
    assert evil.headers.get("access-control-allow-origin") is None


def test_f14_wildcard_never_honoured(make_app):
    from api.main import create_app
    _, repo = make_app()
    app = create_app(repo=repo, storage_root=str(Path(repo.db_path).parent / "s"),
                     schema={}, cors_origins=["*"])
    r = _preflight(TestClient(app), "https://evil.example")
    assert r.headers.get("access-control-allow-origin") is None


# ════════════════════════════════════════════════════════════════════
# F19  hardening headers on the API itself
# ════════════════════════════════════════════════════════════════════

_HARDENING = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
}


def _assert_hardened(r):
    for k, v in _HARDENING.items():
        assert r.headers.get(k) == v, (r.request.url, k, r.headers.get(k))


def test_f19_headers_on_success_error_and_crash(make_app):
    app, _ = make_app()

    @app.get("/_s1b/boom")
    async def _boom():
        raise RuntimeError("boom")

    c = TestClient(app, raise_server_exceptions=False)
    _assert_hardened(c.get("/healthz"))
    _assert_hardened(c.get("/auth/me"))            # 401 from HTTPException
    _assert_hardened(c.get("/definitely-not-a-route"))
    crash = c.get("/_s1b/boom")
    assert crash.status_code == 500
    _assert_hardened(crash)


# ════════════════════════════════════════════════════════════════════
# F24 / F25  OIDC PKCE + GitHub scope
# ════════════════════════════════════════════════════════════════════

_DISCOVERY = {
    "google": {"issuer": "https://accounts.google.com",
               "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
               "token_endpoint": "https://oauth2.googleapis.com/token",
               "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"},
    "github": {"issuer": "https://github.com",
               "authorization_endpoint": "https://github.com/login/oauth/authorize",
               "token_endpoint": "https://github.com/login/oauth/access_token",
               "jwks_uri": "https://github.com/.well-known/jwks"},
}


@pytest.fixture()
def oidc_app(make_app, monkeypatch):
    for p in ("GOOGLE", "GITHUB"):
        monkeypatch.setenv(f"{p}_OIDC_CLIENT_ID", f"{p.lower()}-client")
        monkeypatch.setenv(f"{p}_OIDC_CLIENT_SECRET", f"{p.lower()}-secret")
    monkeypatch.setenv("VIDEO_FACTORY_PUBLIC_BASE_URL", "http://testserver")
    import api.oidc as _oidc
    _oidc._reset_for_tests()

    def _fake_get(provider, *, force_refresh=False):
        return {"loaded_at": time.time(), "discovery": _DISCOVERY[provider],
                "jwks": {"keys": []}, "client_id": f"{provider}-client",
                "client_secret": f"{provider}-secret",
                "discovery_url": f"https://example/{provider}"}
    monkeypatch.setattr(_oidc._provider_cache, "get", _fake_get)
    app, repo = make_app()
    return app, repo, _oidc


def _start(c, provider="google"):
    r = c.get(f"/auth/oauth/{provider}/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    return parse_qs(urlparse(r.headers["Location"]).query)


def test_f24_authorize_url_carries_s256_challenge_bound_to_stored_verifier(oidc_app):
    app, repo, _oidc = oidc_app
    qs = _start(TestClient(app))
    assert qs["code_challenge_method"] == ["S256"]
    challenge = qs["code_challenge"][0]
    row = repo._conn().execute(
        "SELECT code_verifier FROM oidc_pending WHERE state=?", (qs["state"][0],)).fetchone()
    verifier = row[0]
    assert 43 <= len(verifier) <= 128
    assert re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier)
    assert verifier not in json.dumps(qs), "verifier must never leave the server"
    import base64
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expect


def test_f24_callback_sends_the_bound_verifier(oidc_app, monkeypatch):
    app, repo, _oidc = oidc_app
    c = TestClient(app)
    qs = _start(c)
    seen = {}

    def _rec(*, provider, code, redirect_uri, code_verifier=None):
        seen["code_verifier"] = code_verifier
        raise _oidc.OIDCCallbackError("stop here")
    monkeypatch.setattr(_oidc, "_exchange_code", _rec)
    r = c.get(f"/auth/oauth/google/callback?code=abc&state={qs['state'][0]}")
    assert r.status_code == 400
    import base64
    v = seen["code_verifier"]
    assert v
    assert base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode() \
        == qs["code_challenge"][0]


def test_f24_token_request_body_contains_code_verifier(oidc_app, monkeypatch):
    app, repo, _oidc = oidc_app
    import httpx
    captured = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"access_token": "t"}

    def _post(url, content=None, **kw):
        captured["body"] = parse_qs(content)
        return _Resp()
    monkeypatch.setattr(httpx, "post", _post)
    _oidc._exchange_code(provider="google", code="c", redirect_uri="http://x/cb",
                         code_verifier="v" * 50)
    assert captured["body"]["code_verifier"] == ["v" * 50]


def test_f24_pending_row_without_verifier_is_refused(oidc_app, monkeypatch):
    app, repo, _oidc = oidc_app
    c = TestClient(app)
    _start(c)  # creates the table
    repo._conn().execute(
        "INSERT INTO oidc_pending(state, nonce, provider, redirect_after, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?)", ("legacy.state", "n", "google", "", "", _future_iso(300)))
    repo._conn().commit()
    called = []
    monkeypatch.setattr(_oidc, "_exchange_code", lambda **kw: called.append(kw) or {})
    r = c.get("/auth/oauth/google/callback?code=abc&state=legacy.state")
    assert r.status_code == 400
    assert called == [], "code exchanged without a PKCE verifier"


def test_f25_github_scope_is_user_email_only(oidc_app):
    app, _, _ = oidc_app
    qs = _start(TestClient(app), "github")
    assert qs["scope"] == ["user:email"]
    assert qs["code_challenge_method"] == ["S256"]


def test_f8_oidc_sign_in_rotates_presented_session(oidc_app, monkeypatch):
    app, repo, _oidc = oidc_app
    uid = _signup(TestClient(app), email="gh@example.com").json()["user_id"]
    planted = _plant_session(repo, uid)
    c = TestClient(app)
    qs = _start(c, "github")
    monkeypatch.setattr(_oidc, "_exchange_code", lambda **kw: {"access_token": "tok"})
    import httpx

    class _R:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    def _get(url, **kw):
        if url.endswith("/user"):
            return _R({"id": 4242, "login": "gh"})
        return _R([{"email": "gh@example.com", "primary": True, "verified": True}])
    monkeypatch.setattr(httpx, "get", _get)
    r = c.get(f"/auth/oauth/github/callback?code=abc&state={qs['state'][0]}",
              headers=_cookie(planted))
    assert r.status_code == 200, r.text
    assert r.cookies.get("vf_session") not in (None, planted)
    assert repo.get_session(planted) is None


# ════════════════════════════════════════════════════════════════════
# F29  session secret rotation
# ════════════════════════════════════════════════════════════════════

def test_f29_previous_secret_verifies_but_never_signs(make_app, monkeypatch):
    app, repo = _app_with_csrf_probe(make_app)
    uid = _signup(TestClient(app)).json()["user_id"]
    sid = _plant_session(repo, uid)
    old_token = _csrf(sid, SECRET)
    new_secret = "s1b-rotated-secret-" + "b" * 40
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", new_secret)
    c = TestClient(app)
    h = {**_cookie(sid), "X-CSRF-Token": old_token}
    assert c.post("/_s1b/csrf-probe", headers=h).status_code == 403
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET_PREVIOUS", SECRET)
    assert c.post("/_s1b/csrf-probe", headers=h).status_code == 200
    from auth.session import derive_csrf_token
    assert derive_csrf_token(sid) == _csrf(sid, new_secret) != old_token
    # Previous secret also keeps the SvelteKit internal token valid.
    from auth.session import verify_internal_token
    old_internal = hmac.new(SECRET.encode(), b"vf-internal-caller:v1",
                            hashlib.sha256).hexdigest()
    assert verify_internal_token(old_internal)
    monkeypatch.delenv("VIDEO_FACTORY_SESSION_SECRET_PREVIOUS")
    assert not verify_internal_token(old_internal)


def test_f29_rotation_documented():
    doc = (REPO_ROOT / "docs/operations-prod.md").read_text()
    assert "VIDEO_FACTORY_SESSION_SECRET_PREVIOUS" in doc


# ════════════════════════════════════════════════════════════════════
# A2 F-05  Secure cookie
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("env", ["production", "staging"])
def test_a2_f05_secure_cookie_in_deployed_envs(make_app, monkeypatch, env):
    monkeypatch.setenv("VIDEO_FACTORY_ENV", env)
    app, _ = make_app()
    r = _signup(TestClient(app), headers={"Origin": "http://testserver"})
    assert r.status_code == 200, r.text
    assert "Secure" in r.headers["set-cookie"]


def test_a2_f05_secure_cookie_on_https_request(make_app):
    app, _ = make_app()
    r = _signup(TestClient(app, base_url="https://testserver"))
    assert r.status_code == 200, r.text
    assert "Secure" in r.headers["set-cookie"]


def test_a2_f05_secure_cookie_when_trusted_proxy_says_https(make_app):
    app, _ = make_app()
    c = TestClient(app, client=("127.0.0.1", 40000))
    r = _signup(c, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["set-cookie"]
    # The same header from an untrusted peer is ignored.
    c2 = TestClient(app, client=("198.51.100.9", 40000))
    r2 = _signup(c2, email="u2@example.com", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" not in r2.headers["set-cookie"]


def test_a2_f05_compose_sets_production_env():
    src = (REPO_ROOT / "docker-compose.production.yml").read_text()
    api = src.split("\n  web:", 1)[0]
    assert re.search(r"VIDEO_FACTORY_ENV:\s*\"?production\"?", api)
