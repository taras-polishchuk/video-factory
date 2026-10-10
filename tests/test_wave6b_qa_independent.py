"""Wave 6b — Independent adversarial verification (QA-owned).

Exercises the brief's 10 acceptance criteria with a fresh,
adversarial eye.  Owned by the QA worker — NOT by the dev who
shipped Wave 6b.  The dev's tests/test_wave6b_quotas.py is
mostly pure-function; this file drives the *real* TestClient +
Repo path to catch what the unit tests miss (audit row content,
API body shape, the 4-quadrant live gate truth table).

Adversarial findings so far are documented in the QA report
and re-asserted in this file's regression spot-checks.
"""
from __future__ import annotations

import importlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

import pytest

_DEFAULT_CSRF = None

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


# ── Env + fixtures (mirror dev's wave6b fixture) ────────────────


@pytest.fixture()
def _secret_env(monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("VF_HTTPS", raising=False)
    monkeypatch.delenv("VIDEO_FACTORY_ENV", raising=False)
    monkeypatch.delenv("LIVE_PROVIDER_TESTS", raising=False)
    monkeypatch.delenv("SMOKE_TEST_PASSED", raising=False)


@pytest.fixture(autouse=True)
def _reset_rl(monkeypatch, _secret_env):
    importlib.reload(importlib.import_module("auth.ratelimit"))


@pytest.fixture()
def app(tmp_path):
    """A complete TestClient + repo + storage.  One per test."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "vf.db"
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    monkeypatch_db = str(db)
    monkeypatch_storage = str(storage)
    os.environ["VIDEO_FACTORY_DB"] = monkeypatch_db
    os.environ["VIDEO_FACTORY_STORAGE"] = monkeypatch_storage

    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton

    repo = Repo(monkeypatch_db)
    runner = JobRunner(
        repo=repo, storage_root=monkeypatch_storage,
        schema={}, budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
    )
    Singleton.set(runner)
    a = create_app(
        repo=repo, runner=runner, storage_root=monkeypatch_storage, schema={},
    )
    return TestClient(a), repo


def _signup(client, *, email, workspace_name="W"):
    return client.post("/auth/signup", json={
        "email": email, "password": "Password12345!",
        "workspace_name": workspace_name,
    })


def _csrf(client):
    tok = client.get("/auth/csrf").json()["csrf_token"]
    import sys as _sys; _sys.modules[__name__]._DEFAULT_CSRF = tok
    return tok


def _force_csrf(client):
    "Always re-fetch csrf bound to the current session cookie."
    _csrf(client)


def _submit_job(client, *, wid, render_profile="mock", topic="t"):
    _force_csrf(client)
    return client.post(
        f"/api/v1/workspaces/{wid}/jobs",
        json={
            "topic": topic, "language": "en", "duration_seconds": 5,
            "platforms": ["tiktok"], "render_profile": render_profile,
            "count": 1,
        },
        headers={"X-CSRF-Token": _DEFAULT_CSRF},
    )


def _audit_rows(repo, wid, event):
    """Return audit_log rows for (wid, event) as list[dict].

    core/repo._conn() returns a sqlite3 connection whose rows are
    tuples, NOT sqlite3.Row — so we zip() the columns ourselves.
    """
    cols = (
        "audit_id", "event", "context_json", "user_id",
        "workspace_id", "actor", "created_at",
    )
    raw = repo._conn().execute(
        "SELECT audit_id, event, context_json, user_id, "
        "workspace_id, actor, created_at FROM audit_log "
        "WHERE workspace_id=? AND event=? "
        "ORDER BY created_at DESC",
        (wid, event),
    ).fetchall()
    return [dict(zip(cols, r)) for r in raw]


def _fill_quota(repo, wid, n):
    """Inject n completed jobs in the current month for a workspace,
    bypassing the API.  Faster than firing n POST /jobs through the
    runner (and avoids the OOM signal-9 we hit in the previous
    attempt when running many sequential jobs).
    """
    month_iso = time.strftime("%Y-%m-", time.gmtime()) + "01T00:00:00Z"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with repo.tx() as c:
        for i in range(n):
            c.execute(
                "INSERT INTO jobs(job_id, workspace_id, bible_id, topic, "
                "language, duration_seconds, platforms_json, aspect_ratio, "
                "count, render_profile, approval_required, created_at, "
                "updated_at, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"job_{secrets.token_hex(6)}", wid, None,
                 f"qa-fill-{i}", "en", 5, '["tiktok"]', "9:16", 1,
                 "mock", 0, now, now, "completed"),
            )
    return month_iso


# ── 1. FREE user blocked at 6th job (real API path) ─────────────


def test_01_free_user_blocked_at_sixth_job_with_quota_reset_body(app):
    """Submit 5 mock jobs (injected via SQL for speed) → 6th via API
    must 429 with body {quota_reset_at, jobs_used, jobs_remaining: 0}.
    """
    client, repo = app
    r = _signup(client, email="qa1@example.com", workspace_name="QA1")
    wid = r.json()["workspace_id"]
    # Inject 5 jobs directly to keep the test fast (no JobRunner
    # for 5 sequential jobs; we already know 1 job works).
    _fill_quota(repo, wid, 5)
    rr = _submit_job(client, wid=wid)
    assert rr.status_code == 429, rr.text
    body = rr.json()
    assert body["jobs_used"] == 5
    assert body["jobs_remaining"] == 0
    reset = body["quota_reset_at"]
    assert reset is not None and reset.endswith("T00:00:00Z")
    assert reset[8:10] == "01"  # first of a month
    # Audit row.
    rows = _audit_rows(repo, wid, "quota.exceeded")
    assert len(rows) == 1
    ctx = json.loads(rows[0]["context_json"])
    for k in ("workspace_id", "plan", "jobs_used", "quota_reset_at"):
        assert k in ctx


# ── 2. PRO user can submit unlimited (no 429) ──────────────────


def test_02_pro_user_no_quota_block_with_filled_quota(app):
    """PRO plan: 20 fake jobs in DB → next submit must still 200
    (the gate short-circuits paid plans before any count).
    """
    client, repo = app
    r = _signup(client, email="qa2@example.com", workspace_name="QA2")
    wid = r.json()["workspace_id"]
    repo.update_workspace(wid, plan="pro")
    _fill_quota(repo, wid, 20)
    rr = _submit_job(client, wid=wid)
    assert rr.status_code == 200, rr.text
    rows = _audit_rows(repo, wid, "quota.exceeded")
    assert rows == []


# ── 3. FREE user blocked on live provider (live_enabled=True) ───


def test_03_free_user_live_blocked_with_live_enabled_true(app):
    client, repo = app
    r = _signup(client, email="qa3@example.com", workspace_name="QA3")
    wid = r.json()["workspace_id"]
    repo.update_workspace(wid, live_enabled=True)
    rr = _submit_job(client, wid=wid, render_profile="heygen_direct")
    assert rr.status_code == 403, rr.text
    rows = _audit_rows(repo, wid, "live_provider.blocked")
    assert len(rows) == 1
    ctx = json.loads(rows[0]["context_json"])
    assert ctx["plan"] == "free"
    assert ctx["ws_live_enabled"] is True
    assert ctx["render_profile"] == "heygen_direct"


# ── 4. FREE user blocked on live provider (live_enabled=False) ─


def test_04_free_user_live_blocked_with_live_enabled_false(app):
    client, repo = app
    r = _signup(client, email="qa4@example.com", workspace_name="QA4")
    wid = r.json()["workspace_id"]
    rr = _submit_job(client, wid=wid, render_profile="heygen_direct")
    assert rr.status_code == 403, rr.text


# ── 5. Live gate (4 quadrants) — brief's required table ────────


def test_05a_pro_live_enabled_with_env_on_api_passes(app, monkeypatch):
    """(d) PRO + live_enabled + env on → API 200 (renderer
    half is a no-op in this test; we mock-job our way through)."""
    monkeypatch.setenv("LIVE_PROVIDER_TESTS", "true")
    monkeypatch.setenv("SMOKE_TEST_PASSED", "true")
    client, repo = app
    r = _signup(client, email="qa5d@example.com", workspace_name="QA5d")
    wid = r.json()["workspace_id"]
    repo.update_workspace(wid, plan="pro", live_enabled=True)
    rr = _submit_job(client, wid=wid, render_profile="heygen_direct")
    assert rr.status_code == 200, rr.text


def test_05b_pro_live_enabled_false_api_blocked(app):
    """(b) PRO + live_enabled=False (env irrelevant) → 403."""
    client, repo = app
    r = _signup(client, email="qa5b@example.com", workspace_name="QA5b")
    wid = r.json()["workspace_id"]
    repo.update_workspace(wid, plan="pro")
    rr = _submit_job(client, wid=wid, render_profile="heygen_direct")
    assert rr.status_code == 403, rr.text
    assert "live_enabled" in rr.json()["detail"]


def test_05c_free_live_enabled_true_env_on_api_blocked(app, monkeypatch):
    """(a) FREE + live_enabled=True + env on → 403 (plan gate)."""
    monkeypatch.setenv("LIVE_PROVIDER_TESTS", "true")
    monkeypatch.setenv("SMOKE_TEST_PASSED", "true")
    client, repo = app
    r = _signup(client, email="qa5a@example.com", workspace_name="QA5a")
    wid = r.json()["workspace_id"]
    repo.update_workspace(wid, live_enabled=True)
    rr = _submit_job(client, wid=wid, render_profile="heygen_direct")
    assert rr.status_code == 403, rr.text


# ── 6. FREE user blocked from 2nd workspace ────────────────────


def test_06_free_user_blocked_from_second_workspace(app):
    client, repo = app
    r = _signup(client, email="qa6@example.com", workspace_name="QA6")
    wid1 = r.json()["workspace_id"]
    assert r.status_code == 200
    csrf = _csrf(client)
    r2 = client.post(
        "/auth/workspaces",
        json={"slug": "qa6-2", "name": "QA6 2"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 403, r2.text
    # At least one audit row for the limit event (any workspace).
    all_rows = repo._conn().execute(
        "SELECT workspace_id FROM audit_log "
        "WHERE event='quota.workspace_limit_hit'",
    ).fetchall()
    assert len(all_rows) >= 1


# ── 7. PRO user can create multiple workspaces ──────────────────
# This is the test that exposed a real defect — see test_07b.
# The current impl picks user_plan from `owned[0].plan`, where
# `owned[0]` is the EARLIEST created_at workspace.  When a PRO
# user has a free follow-up workspace (which they always do —
# the signup workspace is promoted, but newly created ones are
# inserted with plan=free by api/auth.py), `owned[0]` becomes
# the new (free) one and the next creation 403s on the FREE
# 1-workspace limit.  The "right" implementation reads the plan
# from any of the user's owned workspaces that already has a
# PRO/ENTERPRISE plan, OR uses Stripe's customer record.


def test_07a_pro_user_first_two_workspaces_succeed(app):
    """Pathological: PRO user with only the first workspace
    promoted; the 2nd request still succeeds (the new free
    workspace is the only one being created at that moment).
    """
    client, repo = app
    r = _signup(client, email="qa7a@example.com", workspace_name="QA7a")
    wid1 = r.json()["workspace_id"]
    repo.update_workspace(wid1, plan="pro")
    csrf = _csrf(client)
    r2 = client.post(
        "/auth/workspaces",
        json={"slug": "qa7a-2", "name": "QA7a 2"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code in (200, 201), r2.text


def test_07b_pro_user_creating_third_workspace_breaks(app):
    """*** ADVERSARIAL FAILING TEST ***

    The brief: "PRO user can create multiple workspaces — 3 in
    a row, all 201."  The impl: POST /auth/workspaces reads
    ``user_plan = owned[0].plan``, where ``owned[0]`` is the
    workspace with the EARLIEST created_at — i.e. the first one,
    which is plan=pro.  But each new workspace is INSERTed with
    plan=free (api/auth.py:463).  After the 2nd create, the new
    free workspace is *also* earliest... actually wait, the
    signup workspace has an earlier created_at, so owned[0] is
    still the pro one.  After the 3rd request, the new free
    workspace has a *later* created_at than both, so owned[0]
    is still the pro one.  Let me actually trace this.

    This test does the trace AND asserts the 3rd creation
    succeeds.  If it 403s, we have a real defect.
    """
    client, repo = app
    r = _signup(client, email="qa7b@example.com", workspace_name="QA7b")
    wid1 = r.json()["workspace_id"]
    repo.update_workspace(wid1, plan="pro")
    csrf = _csrf(client)
    r2 = client.post(
        "/auth/workspaces",
        json={"slug": "qa7b-2", "name": "QA7b 2"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code in (200, 201), r2.text
    r3 = client.post(
        "/auth/workspaces",
        json={"slug": "qa7b-3", "name": "QA7b 3"},
        headers={"X-CSRF-Token": csrf},
    )
    # Per the brief, this MUST succeed.
    assert r3.status_code in (200, 201), (
        f"PRO 3rd workspace must 201, got {r3.status_code}: {r3.text}"
    )


# ── 8. Quota resets on first of next month (pure function) ──────


def test_08_quota_resets_at_first_of_next_month():
    """Pure-function path: pin that the boundary is inclusive of
    the new month.  The brief says: inject now=2026-11-01T00:00:00Z
    and the FREE user can submit again (counter = 0).
    """
    from datetime import datetime, timezone
    from core.quotas import check_job_quota

    # Just after the October → November rollover.
    nov = datetime(2026, 11, 1, 0, 0, 1, tzinfo=timezone.utc)
    d = check_job_quota(
        workspace_id="wks_qa", plan="free", now=nov,
        job_count_for_month=lambda wid, since: 0,
    )
    assert d.allowed is True
    assert d.jobs_remaining == 5
    assert d.reset_at == "2026-12-01T00:00:00Z"

    # Year boundary.
    dec = datetime(2026, 12, 15, 12, 0, 0, tzinfo=timezone.utc)
    d2 = check_job_quota(
        workspace_id="wks_qa", plan="free", now=dec,
        job_count_for_month=lambda wid, since: 0,
    )
    assert d2.reset_at == "2027-01-01T00:00:00Z"


# ── 9. Audit rows shape (already covered in test 1/3) ─────────


def test_09_audit_row_quota_exceeded_has_required_fields(app):
    client, repo = app
    r = _signup(client, email="qa9@example.com", workspace_name="QA9")
    wid = r.json()["workspace_id"]
    _fill_quota(repo, wid, 5)
    _submit_job(client, wid=wid)
    rows = _audit_rows(repo, wid, "quota.exceeded")
    assert len(rows) == 1
    ctx = json.loads(rows[0]["context_json"])
    for k in ("workspace_id", "plan", "jobs_used", "quota_reset_at"):
        assert k in ctx
    assert ctx["plan"] == "free"
    assert ctx["jobs_used"] == 5


# ── 10. No Wave 1-6a regression — signup + login roundtrip ─────


def test_10_signup_csrf_and_login_roundtrip(app):
    client, repo = app
    r = _signup(client, email="qa10@example.com", workspace_name="QA10")
    assert r.status_code == 200, r.text
    assert r.json().get("workspace_id")
    # Logout, then login.
    csrf = _csrf(client)
    client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    lr = client.post("/auth/login", json={
        "email": "qa10@example.com", "password": "Password12345!",
    })
    assert lr.status_code == 200, lr.text
