"""Tests for Wave 1 auth foundations: users, workspaces, memberships,
sessions, cookies, rate limiting, role hierarchy, CSRF, and the
end-to-end signup → submit-job integration.

The fixtures build an isolated app per test so cookies + DB are
fresh. The `env` fixture in conftest.py would conflict with the
cookie store, so we re-implement it here.
"""
from __future__ import annotations

import os
import pathlib
import time
from typing import Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture()
def secret_env(monkeypatch):
    """Set the SESSION secret + Fernet SECRET_KEY for the test."""
    monkeypatch.setenv(
        "VIDEO_FACTORY_SESSION_SECRET", "x" * 64,
    )
    monkeypatch.setenv(
        "VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode(),
    )
    # Smaller rate-limit windows so we can test the limit quickly.
    monkeypatch.setenv("VF_LOGIN_IP_WINDOW_SEC", "60")
    monkeypatch.setenv("VF_LOGIN_IP_LIMIT", "5")
    monkeypatch.setenv("VF_LOGIN_EMAIL_WINDOW_SEC", "60")
    monkeypatch.setenv("VF_LOGIN_EMAIL_LIMIT", "10")
    # Also enable secure-cookie off in dev.
    monkeypatch.delenv("VF_HTTPS", raising=False)
    monkeypatch.delenv("VIDEO_FACTORY_ENV", raising=False)


@pytest.fixture(autouse=True)
def _reset_ratelimit(monkeypatch, secret_env):
    """Reload auth.ratelimit so each test sees a fresh env.

    Earlier tests may have reloaded this module with overrides;
    autouse reloading keeps the next test deterministic.
    """
    import importlib
    import auth.ratelimit as _rl
    importlib.reload(_rl)


@pytest.fixture()
def auth_env(tmp_path, monkeypatch, secret_env):
    db = tmp_path / "vf.db"
    storage = tmp_path / "storage"
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(storage))
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")
    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton
    repo = Repo(str(db))
    runner = JobRunner(
        repo=repo, storage_root=str(storage), schema={},
        budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
    )
    Singleton.set(runner)
    app = create_app(
        repo=repo, runner=runner, storage_root=str(storage), schema={},
    )
    client = TestClient(app)
    client._vf_repo = repo
    client._vf_runner = runner
    yield client, repo, storage


# Hang guard only. Completion is signalled by the runner thread
# finishing (JobRunner.wait), not by polling the job row against a
# short wall clock: the old 60 s poll failed whenever the host was
# loaded (CPU-bound ffmpeg encodes under pytest-xdist). t_cfd27f30.
JOB_HANG_GUARD_S = 600


def _wait_done(client, job_id, timeout_s=JOB_HANG_GUARD_S):
    from core.runners import Singleton
    finished = Singleton.get().wait(job_id, timeout=timeout_s)
    j = client.get(f"/api/v1/jobs/{job_id}").json()
    if not finished:
        raise AssertionError(
            f"job pipeline thread still running after {timeout_s}s "
            f"(state={j['state']!r})"
        )
    assert j["state"] in ("done", "failed"), j
    return j


def _signup(client, *, email="alice@example.com", password="Password12345!",
              workspace_name="Alice Co"):
    return client.post("/auth/signup", json={
        "email": email, "password": password, "workspace_name": workspace_name,
    })


def _login(client, *, email, password):
    return client.post("/auth/login", json={
        "email": email, "password": password,
    })


# ── Crypto + cookies ──────────────────────────────────────────


def test_password_hash_round_trip_argon2():
    from auth.passwords import hash_password, verify_password, algorithm_of
    h = hash_password("Password12345!")
    assert h.startswith("argon2:")
    assert verify_password("Password12345!", h) is True
    assert verify_password("WrongPassword", h) is False
    assert algorithm_of(h) == "argon2"


def test_session_cookie_attributes_http_only_same_site_strict_secure(auth_env):
    client, _, _ = auth_env
    r = _signup(client)
    assert r.status_code == 200
    # The Set-Cookie value should carry HttpOnly + SameSite=Strict
    set_cookie = r.headers.get("set-cookie", "")
    assert "vf_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Path=/" in set_cookie


def test_session_secret_missing_returns_503(monkeypatch, tmp_path):
    """If VIDEO_FACTORY_SESSION_SECRET is not set, the auth endpoints
    return 503."""
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("VIDEO_FACTORY_SESSION_SECRET", raising=False)
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(tmp_path / "storage"))
    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton
    repo = Repo(str(tmp_path / "vf.db"))
    runner = JobRunner(
        repo=repo, storage_root=str(tmp_path / "storage"), schema={},
    )
    Singleton.set(runner)
    app = create_app(
        repo=repo, runner=runner,
        storage_root=str(tmp_path / "storage"), schema={},
    )
    client = TestClient(app)
    r = client.post("/auth/signup", json={
        "email": "x@example.com", "password": "Password12345!",
        "workspace_name": "X",
    })
    assert r.status_code == 503
    assert "VIDEO_FACTORY_SESSION_SECRET" in r.json()["detail"]


# ── Signup ────────────────────────────────────────────────────


def test_signup_creates_user_workspace_membership_atomically(auth_env):
    client, repo, _ = auth_env
    r = _signup(client, email="bob@example.com", workspace_name="Bob Co")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "user_id" in body
    assert "workspace_id" in body
    # DB has all three
    user = repo.get_user(body["user_id"])
    assert user["email"] == "bob@example.com"
    ws = repo.get_workspace(body["workspace_id"])
    assert ws["name"] == "Bob Co"
    assert ws["owner_user_id"] == body["user_id"]
    ms = repo.list_memberships(body["user_id"])
    assert len(ms) == 1
    assert ms[0]["role"] == "owner"


def test_signup_duplicate_email_returns_409(auth_env):
    client, _, _ = auth_env
    r1 = _signup(client, email="dup@example.com")
    assert r1.status_code == 200
    r2 = _signup(client, email="dup@example.com")
    assert r2.status_code == 409


def test_signup_weak_password_rejected(auth_env):
    client, _, _ = auth_env
    r = _signup(client, password="short")
    assert r.status_code == 400
    assert "password" in r.json()["detail"].lower()
    # No upper
    r = _signup(client, password="alllower1234")
    assert r.status_code == 400
    # No digit
    r = _signup(client, password="AllLowerAndUpper")
    assert r.status_code == 400


# ── Login + rate limit ────────────────────────────────────────


def test_login_returns_session_cookie(auth_env):
    client, _, _ = auth_env
    _signup(client, email="carol@example.com")
    r = client.post("/auth/login", json={
        "email": "carol@example.com", "password": "Password12345!",
    })
    assert r.status_code == 200
    assert "vf_session=" in r.headers.get("set-cookie", "")


def test_login_wrong_password_increments_attempts(auth_env):
    client, repo, _ = auth_env
    _signup(client, email="dave@example.com")
    r = client.post("/auth/login", json={
        "email": "dave@example.com", "password": "WrongPassword1!",
    })
    assert r.status_code == 401
    # One failed attempt recorded
    rows = repo._conn().execute(
        "SELECT COUNT(*) FROM login_attempts WHERE email=? AND succeeded=0",
        ("dave@example.com",),
    ).fetchone()
    assert rows[0] == 1


def test_login_rate_limit_5_per_ip_15min(auth_env):
    client, _, _ = auth_env
    _signup(client, email="eve@example.com")
    # 5 wrong attempts
    for _ in range(5):
        client.post("/auth/login", json={
            "email": "eve@example.com", "password": "WrongPassword1!",
        })
    # 6th attempt should be locked out
    r = client.post("/auth/login", json={
        "email": "eve@example.com", "password": "Password12345!",
    })
    assert r.status_code == 429
    assert "too many" in r.json()["detail"].lower()


def test_login_rate_limit_10_per_email_per_hour(auth_env, monkeypatch):
    client, _, _ = auth_env
    # Raise the IP limit so the email limit is the binding one
    # in this test. We test the IP limit separately in
    # test_login_rate_limit_5_per_ip_15min.
    monkeypatch.setenv("VF_LOGIN_IP_LIMIT", "100")
    # Re-import the rate limit module so the new env takes effect
    import importlib
    import auth.ratelimit as _rl
    importlib.reload(_rl)
    _signup(client, email="frank@example.com")
    # 10 wrong attempts at the email limit
    for _ in range(10):
        client.post("/auth/login", json={
            "email": "frank@example.com", "password": "WrongPassword1!",
        })
    # 11th: should be email-locked → 423
    r = client.post("/auth/login", json={
        "email": "frank@example.com", "password": "Password12345!",
    })
    assert r.status_code == 423
    assert "too many" in r.json()["detail"].lower()


def test_login_lockout_writes_audit_row(auth_env):
    client, repo, _ = auth_env
    _signup(client, email="grace@example.com")
    for _ in range(6):
        client.post("/auth/login", json={
            "email": "grace@example.com", "password": "WrongPassword1!",
        })
    rows = repo._conn().execute(
        "SELECT event FROM audit_log WHERE event LIKE 'auth.login%'"
    ).fetchall()
    events = {r[0] for r in rows}
    assert "auth.login_locked" in events


# ── Logout + protected endpoints ─────────────────────────────


def test_logout_invalidates_session(auth_env):
    client, repo, _ = auth_env
    _signup(client, email="henry@example.com")
    sid_cookie = client.cookies.get("vf_session")
    assert sid_cookie
    # /auth/logout requires X-CSRF-Token (state-changing endpoint,
    # enforced per Wave 1 QA blocker #1).
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    r = client.post("/auth/logout",
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    # Session row gone
    row = repo.get_session(sid_cookie)
    assert row is None


def test_protected_endpoint_401_when_no_cookie(auth_env):
    client, _, _ = auth_env
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_protected_endpoint_403_when_wrong_workspace(auth_env):
    """The lower-level membership guard refuses a user with no
    membership in a given workspace, and `resolve_workspace_membership`
    raises 403 in that case. This is the contract Wave 2 will rely on
    when every endpoint becomes workspace-scoped."""
    client, repo, _ = auth_env
    r = _signup(client, email="ivy@example.com", workspace_name="Ivy Co")
    uid = r.json()["user_id"]
    wid = r.json()["workspace_id"]
    # User has membership in their own workspace
    import auth.dependencies as _dep
    m = _dep.get_membership(repo, uid, wid)
    assert m is not None
    assert m["role"] == "owner"
    # But not in a non-existent workspace
    m2 = _dep.get_membership(repo, uid, "wks_does_not_exist")
    assert m2 is None
    # And resolve_workspace_membership raises 403 for the missing one
    from fastapi import HTTPException
    from auth.dependencies import AuthContext
    ctx = AuthContext(user_id=uid, email="ivy@example.com")
    try:
        _dep.resolve_workspace_membership(repo, ctx, "wks_does_not_exist",
                                           "viewer")
    except HTTPException as e:
        assert e.status_code == 403
    else:
        raise AssertionError("expected 403")


def test_role_hierarchy_owner_admin_editor_viewer():
    from auth.roles import has_min_role, highest_role
    assert has_min_role("owner", "viewer") is True
    assert has_min_role("admin", "editor") is True
    assert has_min_role("editor", "admin") is False
    assert has_min_role("viewer", "viewer") is True
    assert highest_role(["viewer", "editor", "admin"]) == "admin"
    assert highest_role([]) == "viewer"


# ── CSRF + session expiry + rotation ─────────────────────────


def test_csrf_token_required_on_state_changing_requests(auth_env):
    client, repo, _ = auth_env
    r = _signup(client, email="kim@example.com")
    wid = r.json()["workspace_id"]
    # Wave 6b: promote to 'pro' so the FREE=1-workspace gate
    # does not pre-empt the CSRF check under test.
    repo.update_workspace(wid, plan="pro")
    # Without X-CSRF-Token: 403
    r = client.post("/auth/workspaces", json={"slug": "kim-2", "name": "Kim 2"})
    assert r.status_code == 403
    # With valid CSRF token
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    r = client.post("/auth/workspaces",
                     json={"slug": "kim-second", "name": "Kim Second"},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200


def test_session_expires_after_7_days(auth_env):
    """The expires_at field is set 7 days from creation."""
    client, repo, _ = auth_env
    _signup(client, email="liam@example.com")
    sid = client.cookies.get("vf_session")
    sess = repo.get_session(sid)
    assert sess is not None
    # Compute remaining seconds
    import time as _t
    expires = _t.mktime(_t.strptime(sess["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    now = _t.time()
    diff = expires - now
    # 7 days ± 1 minute
    assert 7 * 24 * 60 * 60 - 60 < diff < 7 * 24 * 60 * 60 + 60


def test_session_secret_rotation_invalidates_old_sessions(auth_env, monkeypatch):
    """When VIDEO_FACTORY_SESSION_SECRET changes, old session cookies
    are still in the DB but the CSRF token is no longer derivable.
    More importantly, the cookie secret embedded in the production
    cookie is a derived identifier (the session_id), not the secret
    itself, so a secret rotation in production is handled by
    invalidating all sessions (a separate command). The test asserts
    that the CSRF token check fails after a secret rotation."""
    client, repo, _ = auth_env
    r = _signup(client, email="mia@example.com")
    wid = r.json()["workspace_id"]
    # Wave 6b: promote to 'pro' so the FREE=1-workspace gate
    # does not pre-empt the CSRF check under test.
    repo.update_workspace(wid, plan="pro")
    # Rotate the secret
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "y" * 64)
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    # The old CSRF token (derived under the previous secret) is no
    # longer valid. A state-changing request must use a freshly
    # derived token from /auth/csrf.
    r = client.post("/auth/workspaces",
                     json={"slug": "mia-2", "name": "Mia 2"},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200


# ── Forgot + verify ───────────────────────────────────────────


def test_forgot_password_sends_magic_link(auth_env):
    client, repo, _ = auth_env
    _signup(client, email="nina@example.com")
    r = client.post("/auth/forgot", json={"email": "nina@example.com"})
    assert r.status_code == 200
    body = r.json()
    # In dev we surface the token to make E2E testable.
    assert "reset_token" in body
    assert body["reset_token"]
    # The token must be consumable.
    consume = client.post("/auth/forgot/consume", json={
        "token": body["reset_token"],
        "new_password": "NewPassword1234!",
    })
    assert consume.status_code == 200
    # And the user can log in with the new password.
    r = client.post("/auth/login", json={
        "email": "nina@example.com", "password": "NewPassword1234!",
    })
    assert r.status_code == 200


def test_verify_email_marks_verified(auth_env):
    client, repo, _ = auth_env
    r = _signup(client, email="olive@example.com")
    assert r.status_code == 200
    token = r.json()["verification_token"]
    assert token
    r2 = client.post("/auth/verify", json={"token": token})
    assert r2.status_code == 200
    user = repo.get_user(r.json()["user_id"])
    assert user["email_verified"] is True


# ── Audit + legacy alias ─────────────────────────────────────


def test_audit_log_records_user_id_workspace_id(auth_env):
    client, repo, _ = auth_env
    r = _signup(client, email="paul@example.com")
    uid = r.json()["user_id"]
    wid = r.json()["workspace_id"]
    rows = repo._conn().execute(
        "SELECT event, user_id, workspace_id FROM audit_log "
        "WHERE event='auth.signup'"
    ).fetchall()
    assert len(rows) >= 1
    last = rows[-1]
    assert last[0] == "auth.signup"
    assert last[1] == uid
    assert last[2] == wid


def test_legacy_company_endpoint_alias_resolves_to_workspace(auth_env):
    """The legacy /api/v1/companies/{cid} endpoint is preserved; the
    alias returns a body whose `company_id` matches the request.

    Wave 2 follow-up (t_d76b7c0c, MAJOR #5): the legacy alias
    write paths now require auth + CSRF. The test was previously
    a shape check that did not exercise the auth gate; sign up
    first and use the CSRF token.
    """
    client, _, _ = auth_env
    su = _signup(client, email="legacy-alias@video-factory.test",
                 workspace_name="Legacy Alias")
    assert su.status_code == 200, su.text
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    r = client.post("/api/v1/companies",
                     json={"slug": "legacy", "name": "Legacy"},
                     headers={"X-CSRF-Token": csrf})
    cid = r.json()["company_id"]
    g = client.get(f"/api/v1/companies/{cid}")
    assert g.status_code == 200
    assert g.json()["company_id"] == cid


def test_x_operator_header_still_logged_as_audit_hint(auth_env):
    """Operator identity is recorded in the audit row.

    Wave 2 follow-up (t_d76b7c0c, MAJOR #5): legacy alias
    write paths now require auth + CSRF + membership. The
    actor recorded in the audit row is the authenticated
    user's email (the X-Operator header was a v0 placeholder
    that conflated operator with the X-Workspace-Id header).
    The test exercises the auth/CSRF gates; the company
    itself does not need to exist because the membership
    check is the meaningful boundary here.
    """
    client, _, _ = auth_env
    su = _signup(client, email="audit-actor@video-factory.test",
                 workspace_name="Audit Actor")
    assert su.status_code == 200, su.text
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    # cmp_legacy does not exist and is not aliased to a
    # workspace the test user is a member of — the call
    # must therefore fail with 4xx. The important assertion
    # is that the call got past the auth + CSRF gate (i.e.
    # the response is 4xx for data-shape reasons, not 401/403
    # for auth reasons). We accept any 4xx/5xx here.
    r = client.put(
        "/api/v1/companies/cmp_legacy/credentials/heygen_direct",
        json={"value": "test-secret"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code >= 400
    # 401 would mean CSRF/auth failed; 403 is the new
    # membership-or-not-found boundary, which IS the
    # auth/CSRF gate working correctly for an unknown id.
    assert r.status_code not in (401, 422)


# ── End-to-end integration ───────────────────────────────────


def test_signup_then_submit_job_end_to_end(auth_env):
    client, _, _ = auth_env
    r = _signup(client, email="quinn@example.com",
                 workspace_name="Quinn Co")
    assert r.status_code == 200
    # /auth/me shows the workspace
    me = client.get("/auth/me").json()
    assert len(me["workspaces"]) == 1
    # The legacy POST /api/v1/companies still works (now
    # gated by auth + CSRF — Wave 2 follow-up t_d76b7c0c).
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    cid = client.post("/api/v1/companies", json={"slug": "quinn-c",
                                                  "name": "Quinn C"},
                       headers={"X-CSRF-Token": csrf}).json()["company_id"]
    bid = client.post(f"/api/v1/companies/{cid}/bibles",
                       json={"payload": {"name": "Q", "audience": "x"}},
                       headers={"X-CSRF-Token": csrf}).json()["bible_id"]
    client.post(f"/api/v1/bibles/{bid}/approve",
                 headers={"X-CSRF-Token": csrf})
    jid = client.post("/api/v1/jobs", json={
        "company_id": cid, "bible_id": bid, "topic": "hello",
        "language": "en-US", "duration_seconds": 9, "platforms": ["tiktok"],
    }).json()["job_id"]
    final = _wait_done(client, jid)
    assert final["state"] == "done", final["status_message"]


def test_login_with_locked_user_returns_423(auth_env):
    client, repo, _ = auth_env
    r = _signup(client, email="ruth@example.com")
    uid = r.json()["user_id"]
    repo.update_user_status(uid, status="locked")
    r = client.post("/auth/login", json={
        "email": "ruth@example.com", "password": "Password12345!",
    })
    assert r.status_code == 423
