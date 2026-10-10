"""S1b follow-up regression tests (t_9a3411f1).

Independent adversarial coverage of the three S1b follow-ups:

  * non-admin workspace owners must be 403'd on
    /api/v1/admin/keys/rotate AND /api/v1/admin/keys/status
    (the admin gate, not the role check),
  * a platform-admin caller must still be able to rotate end-to-end
    (happy path survives the gate),
  * /health latency must stay fast while a rotation is in flight
    (the 842ms stall regression: the async offload via
    ``rotate_async`` + ``asyncio.to_thread`` is real, so the
    asyncio event loop stays free under a slow-rotation hook),
  * wrong ``old_key`` from a platform-admin caller returns
    400/403 (never 500) and does NOT flip the global
    ``key_rotation.failed`` flag.

Mirrors the helper shape from ``tests/test_qa_s1b_session.py`` /
``tests/test_security_s1b.py`` so this file can run with a
narrower pytest collection.  No network.  Every test builds its
own app + DB.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import statistics
import threading
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

SECRET = "s1b-followup-secret-" + "f" * 40
PASSWORD = "Password12345!"
OLD_KEY = "s1b-followup-old-db-key-" + "X" * 20
NEW_KEY = "s1b-followup-new-db-key-" + "Y" * 20

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
    "VIDEO_FACTORY_PLATFORM_ADMINS",
)


# ── fixtures / helpers ───────────────────────────────────────────────

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
        repo = (Repo(str(tmp_path / "vf.db"), db_key=db_key) if db_key
                else Repo(str(tmp_path / "vf.db")))
        runner = JobRunner(
            repo=repo, storage_root=str(tmp_path / "storage"), schema={},
            budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
        )
        Singleton.set(runner)
        app = create_app(repo=repo, runner=runner,
                         storage_root=str(tmp_path / "storage"), schema={})
        return app, repo
    return _make


def _signup(client, email, *, pw=PASSWORD):
    return client.post("/auth/signup", json={
        "email": email, "password": pw, "workspace_name": "QA Co",
    })


def _sid(r) -> str:
    return r.cookies.get("vf_session")


def _csrf(sid: str) -> str:
    return hmac.new(SECRET.encode(), sid.encode(), hashlib.sha256).hexdigest()


def _ck(sid: str) -> dict:
    return {"Cookie": f"vf_session={sid}"}


def _owner(app, email="owner@example.com"):
    """Plain workspace owner. NOT on the platform-admin allow-list."""
    c = TestClient(app)
    r = _signup(c, email)
    assert r.status_code == 200, r.text
    return _sid(r), r.json()["user_id"]


def _platform_admin(app, email="admin@example.com"):
    """Sign up + add to VIDEO_FACTORY_PLATFORM_ADMINS."""
    c = TestClient(app)
    r = _signup(c, email)
    assert r.status_code == 200, r.text
    uid = r.json()["user_id"]
    sid = _sid(r)
    existing = os.environ.get("VIDEO_FACTORY_PLATFORM_ADMINS", "")
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    if uid not in parts:
        parts.append(uid)
    os.environ["VIDEO_FACTORY_PLATFORM_ADMINS"] = ",".join(parts)
    return c, uid, sid


def _rotate(app, sid, body, *, csrf=True):
    h = _ck(sid)
    if csrf:
        h["X-CSRF-Token"] = _csrf(sid)
    return TestClient(app).post(
        "/api/v1/admin/keys/rotate", headers=h,
        content=json.dumps(body),
    )


def _count_rows(repo, table, where="1=1", args=()):
    try:
        return repo._conn().execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
    except Exception:
        return 0


# ════════════════════════════════════════════════════════════════════
# 1. Non-admin workspace owner → 403 on rotate AND status
# ════════════════════════════════════════════════════════════════════
#
# The admin gate is the OUTER boundary on both endpoints.  A user
# who is a workspace owner (passes the role check) but is NOT on
# VIDEO_FACTORY_PLATFORM_ADMINS must be 403'd with a body that
# names the boundary.  This is the contract the fix added; the
# prior shape was an owner could rotate with a valid session,
# which is exactly the cross-tenant re-key the user wanted to
# close.

def test_followup_non_admin_workspace_owner_403_on_rotate(make_app):
    """Plain owner (admin allow-list empty) is 403 on /rotate."""
    app, repo = make_app(db_key=OLD_KEY)
    sid, _uid = _owner(app, "regular-owner@example.com")
    # Sanity: the allow-list is empty so the gate fires for everyone.
    assert os.environ.get("VIDEO_FACTORY_PLATFORM_ADMINS", "") == ""
    r = _rotate(app, sid, {
        "old_key": OLD_KEY, "new_key": NEW_KEY, "current_password": PASSWORD,
    })
    assert r.status_code == 403, (r.status_code, r.text)
    assert "platform admin" in r.text, r.text
    # The DB was NOT re-keyed: the request never reached rotate().
    assert repo.db_key == OLD_KEY


def test_followup_non_admin_workspace_owner_403_on_status(make_app):
    """Plain owner is also 403 on /status (same outer gate)."""
    app, _ = make_app()
    sid, _uid = _owner(app, "regular-owner-2@example.com")
    assert os.environ.get("VIDEO_FACTORY_PLATFORM_ADMINS", "") == ""
    r = TestClient(app).get("/api/v1/admin/keys/status", headers=_ck(sid))
    assert r.status_code == 403, (r.status_code, r.text)
    assert "platform admin" in r.text, r.text


# ════════════════════════════════════════════════════════════════════
# 2. Platform-admin caller can still rotate successfully
# ════════════════════════════════════════════════════════════════════
#
# Once the gate is in place the happy path MUST still work end to
# end: signup → admin allow-list → re-auth → rotate → repo.db_key
# is now the new key.  This is the regression guard against
# closing the cross-tenant hole by mistake.

def test_followup_platform_admin_can_rotate_end_to_end(make_app):
    """Admin allow-list + valid old_key → 200 + repo.db_key changes."""
    app, repo = make_app(db_key=OLD_KEY)
    _, _uid, sid = _platform_admin(app, "admin@example.com")
    # Sanity: the helper put the admin on the allow-list.
    assert _uid in os.environ["VIDEO_FACTORY_PLATFORM_ADMINS"]
    r = _rotate(app, sid, {
        "old_key": OLD_KEY, "new_key": NEW_KEY, "current_password": PASSWORD,
    })
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body.get("completed") is True, body
    assert repo.db_key == NEW_KEY, "repo was not re-keyed after a 200"
    # The follow-up signup must work too — repo usable after rotation.
    after = TestClient(app).post("/auth/signup", json={
        "email": "after-rotation@example.com",
        "password": PASSWORD, "workspace_name": "After",
    })
    assert after.status_code == 200, after.text


# ════════════════════════════════════════════════════════════════════
# 3. /health stays fast while a rotation is in flight
# ════════════════════════════════════════════════════════════════════
#
# Regression: the original (sync) rotate held the worker thread
# AND, because asyncio.run_until_complete in TestClient serializes
# the event loop, a slow rotation stalled the whole request loop
# — /health p95 measured ~842ms.  The fix is rotate_async which
# dispatches the lock-holding work to asyncio.to_thread, leaving
# the event loop free.
#
# We assert the contract: install a RotationHook that sleeps ~1.5s
# INSIDE repo._lock, fire a rotate() request, while it is in
# flight fire 50 concurrent /health probes, and check /health p95
# stays well under the original stall budget.

def test_followup_health_latency_stays_fast_during_slow_rotation(make_app, monkeypatch):
    """While a slow rotation runs, /health latency stays well under the original 842ms stall."""
    from control_plane import key_rotation

    app, repo = make_app(db_key=OLD_KEY)
    _, _uid, sid = _platform_admin(app, "admin-loop@example.com")
    c = TestClient(app)

    # ── 1. Patch rotate_async to inject a slow RotationHook ──
    # The admin handler does NOT pass instrumentation through;
    # we wrap key_rotation.rotate_async so every call runs with a
    # before_copy hook that sleeps `slow_seconds` INSIDE
    # repo._lock, simulating a slow SQLCipher re-key.
    slow_seconds = 1.5
    hook_state: dict = {"calls": 0}
    hook_holder: dict = {"hook": None}   # exposed for debugging

    original_rotate_async = key_rotation.rotate_async

    async def slow_rotate_async(*, repo, old_key, new_key, actor_user_id,
                                db_path=None, instrumentation=None):
        # Build a hook that sleeps slow_seconds on the worker
        # thread; the asyncio event loop stays free while this
        # sleeps because asyncio.to_thread dispatched it.
        def _slow_before_copy(*, epoch):  # noqa: ARG001
            hook_state["calls"] += 1
            time.sleep(slow_seconds)
        hook = key_rotation.RotationHook(before_copy=_slow_before_copy)
        hook_holder["hook"] = hook
        return await original_rotate_async(
            repo=repo, old_key=old_key, new_key=new_key,
            actor_user_id=actor_user_id, db_path=db_path,
            instrumentation=hook,
        )

    monkeypatch.setattr(key_rotation, "rotate_async", slow_rotate_async)

    # ── 2. Kick off the rotation on a worker thread ──
    # TestClient is in-process and single-threaded, so we issue the
    # rotation request from a thread that does NOT own the event
    # loop the /health probes will use.  TestClient has its own
    # portal: a fresh client per probe call is the simplest way.
    rotation_done = threading.Event()
    rotation_status: dict = {"code": None, "text": None}

    def _fire_rotation():
        # TestClient is not thread-safe across the same instance,
        # so build a dedicated one for the rotation.
        rc = TestClient(app)
        r = rc.post(
            "/api/v1/admin/keys/rotate", headers={
                **_ck(sid), "X-CSRF-Token": _csrf(sid),
            },
            content=json.dumps({
                "old_key": OLD_KEY, "new_key": NEW_KEY,
                "current_password": PASSWORD,
            }),
        )
        rotation_status["code"] = r.status_code
        rotation_status["text"] = r.text
        rotation_done.set()

    rotation_thread = threading.Thread(target=_fire_rotation, daemon=True)
    rotation_thread.start()

    # ── 3. Wait briefly for the hook to start (rotation reached
    #       repo._lock — the hook call count will rise). ──
    deadline = time.monotonic() + 5.0
    while hook_state["calls"] == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert hook_state["calls"] >= 1, (
        "slow hook never fired — rotation did not reach before_copy"
    )

    # ── 4. While the rotation is mid-flight, hammer /health ──
    # The TestClient can issue requests from a single client;
    # because rotate_async is dispatched to a worker thread, the
    # event loop is NOT held by the rotation, so /health responses
    # return promptly.
    # Sanity: the rotation must still be in flight — if the hook
    # already finished, we're not actually testing the stalled-
    # event-loop scenario.
    assert not rotation_done.is_set(), (
        "rotation finished before /health probes started; "
        "slow_seconds is too short or the hook did not actually sleep"
    )
    samples = []
    n_samples = 50
    hc = TestClient(app)
    probe_window_deadline = time.monotonic() + 3.0
    while len(samples) < n_samples and time.monotonic() < probe_window_deadline:
        t0 = time.perf_counter()
        r = hc.get("/health")
        dt_ms = (time.perf_counter() - t0) * 1000.0
        assert r.status_code == 200, r.text
        samples.append(dt_ms)
    assert len(samples) >= n_samples // 2, (
        f"only {len(samples)} /health probes landed inside the rotation window"
    )

    # ── 5. Wait for the rotation to complete and sanity-check ──
    rotation_thread.join(timeout=10.0)
    assert rotation_done.is_set(), "rotation thread did not complete"
    assert rotation_status["code"] == 200, (
        f"slow rotation did not succeed: {rotation_status}"
    )
    assert repo.db_key == NEW_KEY
    assert hook_state["calls"] == 1, hook_state

    # ── 6. The latency contract: p95 << 842ms ──
    # The original synchronous implementation measured /health p95
    # at ~842ms under a similarly slow rotation; with rotate_async
    # dispatching via asyncio.to_thread the event loop is free so
    # /health should be effectively as fast as it is without any
    # rotation in flight.
    p95 = statistics.quantiles(samples, n=20)[18]   # 95th percentile
    median = statistics.median(samples)
    # Budget: well under the original 842ms stall — we leave a
    # generous 3x headroom and a hard ceiling at 500ms so a
    # regression that brings back the stall fails the test even on
    # noisy CI.
    BUDGET_P95_MS = 500.0
    assert p95 < BUDGET_P95_MS, (
        f"/health p95 = {p95:.1f}ms (median={median:.1f}ms, "
        f"n={len(samples)}) — the original 842ms stall is back; "
        f"rotate_async is no longer freeing the event loop"
    )
    # Sanity: median is healthy too — if median is high the test is
    # not actually exercising a stalled event loop at all.
    assert median < 200.0, (
        f"/health median = {median:.1f}ms — baseline is already "
        f"slow, the slow-rotation setup is not testing what it "
        f"claims (got samples: {samples[:5]}...)"
    )


# ════════════════════════════════════════════════════════════════════
# 4. Wrong old_key from admin caller → 400/403 (never 500) and the
#    global key_rotation.failed flag stays untouched
# ════════════════════════════════════════════════════════════════════
#
# Caller-error path.  The admin gate is in scope on this endpoint
# (the admin allow-list gates the route), so a wrong-old-key
# rejection surfaces as 403 — same shape as the gate.  The
# global key_rotation.failed flag is reserved for real
# infrastructure faults (corruption, IO error, migration crash);
# a caller-error attempt MUST NOT flip it.

def test_followup_admin_wrong_old_key_403_and_no_global_flag_flip(make_app):
    """Wrong old_key from a platform-admin caller → 403, flag untouched."""
    from control_plane.key_rotation import rotation_state
    app, repo = make_app(db_key=OLD_KEY)
    _, _uid, sid = _platform_admin(app, "admin-wrong@example.com")
    # Sanity: caller IS on the allow-list (gate passes; we are
    # inside the rotate() body when BadOldKey fires).
    assert _uid in os.environ["VIDEO_FACTORY_PLATFORM_ADMINS"]
    r = _rotate(app, sid, {
        "old_key": "wrong-" + OLD_KEY, "new_key": NEW_KEY,
        "current_password": PASSWORD,
    })
    # Caller-error path → 403 (gate is in scope).  500 would mean the
    # fix forgot to translate SQLCipher "file is not a database"
    # into BadOldKey before the broad except flipped the flag.
    assert r.status_code == 403, (r.status_code, r.text)
    # Global flag MUST stay untouched — this alarm is reserved for
    # real infrastructure faults.
    state = rotation_state(repo)
    assert state["failed"] is False, (
        "a wrong-old-key attempt flipped the platform-wide "
        "key_rotation.failed flag — caller-error must stay out "
        "of the infra-fault alarm path"
    )
    assert state["in_progress"] is False, (
        "rotation_in_progress must be cleared after a caller-error "
        "rejection so the API is usable again"
    )
    # Audit shape: infra-fault event MUST NOT be emitted.
    assert _count_rows(
        repo, "runner_audit", "event='key.rotation_failed'",
    ) == 0, (
        "key.rotation_failed was emitted on a caller-error path — "
        "infra-fault events must never leak from caller-error"
    )
    # Caller-error event IS emitted (operator grep target).
    assert _count_rows(
        repo, "runner_audit",
        "event='key.rotation_rejected_caller_error'",
    ) >= 1, "no caller-error audit row was written for the BadOldKey path"
    # DB key was NOT changed.
    assert repo.db_key == OLD_KEY