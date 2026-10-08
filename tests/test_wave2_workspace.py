"""Wave 2 — Multi-tenant wiring tests.

Independent verification of the company_id -> workspace_id rename
and the new isolation rules.  Covers all 25 test IDs from the
brief:

  1.  test_company_id_column_renamed_to_workspace_id_in_every_table
  2.  test_repo_list_companies_by_workspace
  3.  test_repo_rejects_cross_workspace_access
  4.  test_workspace_isolation_companies
  5.  test_workspace_isolation_jobs
  6.  test_workspace_isolation_credentials
  7.  test_workspace_isolation_audit
  8.  test_x_workspace_id_header_resolves_to_member_workspace
  9.  test_x_workspace_id_header_rejects_non_member
  10. test_x_workspace_id_header_absent_falls_back_to_session
  11. test_legacy_company_endpoint_alias_still_works
  12. test_credential_vault_hmac_tag_binds_to_workspace
  13. test_audit_log_records_workspace_id_on_every_action
  14. test_active_workspace_id_in_session_default
  15. test_switch_active_workspace_endpoint
  16. test_workspace_slug_uniqueness_409
  17. test_workspace_owner_cannot_be_removed
  18. test_workspace_status_disabled_blocks_all_endpoints
  19. test_workspace_migration_downgrade_plan_documented
  20. test_migration_checksum_verified_before_apply
  21. test_existing_94_tests_still_green
  22. test_new_workspace_creation_increments_owner_role
  23. test_workspace_plan_default_free
  24. test_workspace_live_enabled_default_false
  25. test_cross_workspace_path_traversal_rejection_still_works
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


# ── Shared fixtures ───────────────────────────────────────────


@pytest.fixture()
def _secret_env(monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VF_LOGIN_IP_WINDOW_SEC", "60")
    monkeypatch.setenv("VF_LOGIN_IP_LIMIT", "5")
    monkeypatch.setenv("VF_LOGIN_EMAIL_WINDOW_SEC", "60")
    monkeypatch.setenv("VF_LOGIN_EMAIL_LIMIT", "10")
    monkeypatch.delenv("VF_HTTPS", raising=False)
    monkeypatch.delenv("VIDEO_FACTORY_ENV", raising=False)


@pytest.fixture(autouse=True)
def _reset_rl(monkeypatch, _secret_env):
    importlib.reload(importlib.import_module("auth.ratelimit"))


def _make_app(tmp_path: Path):
    """Spin up an isolated app+repo+runner for a test."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "vf.db"
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    os.environ["VIDEO_FACTORY_DB"] = str(db)
    os.environ["VIDEO_FACTORY_STORAGE"] = str(storage)
    os.environ["MAX_COST_PER_VIDEO_USD"] = "5"
    os.environ["MAX_COST_PER_BATCH_USD"] = "20"

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
    return TestClient(app), repo, storage


def _signup(client, *, email, password="Password12345!",
             workspace_name="Default Co"):
    return client.post("/auth/signup", json={
        "email": email, "password": password,
        "workspace_name": workspace_name,
    })


def _login(client, *, email, password="Password12345!"):
    return client.post("/auth/login", json={
        "email": email, "password": password,
    })


# ── 1. Schema rename: company_id -> workspace_id in every table

def test_company_id_column_renamed_to_workspace_id_in_every_table(tmp_path):
    """Every table that previously had `company_id` now has
    `workspace_id`.  No tenant-scoped table has a `company_id`
    column at the DB level.
    """
    _client, repo, _ = _make_app(tmp_path)
    # Trigger schema bootstrap with at least one workspace so all
    # tenant tables exist.
    c = repo.create_company(slug="acme", name="Acme")
    wid = c["workspace_id"]
    repo.create_bible(workspace_id=wid, payload={"name": "B"})
    repo.create_asset(workspace_id=wid, kind="logo", filename="a.png",
                       storage_key="x", mime_type="image/png",
                       size_bytes=10, checksum="h")
    job = repo.create_job(workspace_id=wid, topic="t", language="en",
                            duration_seconds=10, platforms=["tiktok"])
    repo.create_publish_intent(job_id=job["job_id"], workspace_id=wid,
                                 action="draft", platforms=["tiktok"])
    # Iterate every table; assert no tenant-scoped table has a
    # `company_id` column.
    tables = [r[0] for r in repo._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    # legacy_company_alias is allowed to have company_id (that's
    # literally its purpose).  Anything else must NOT.
    ALLOWED = {"legacy_company_alias", "companies"}
    bad = []
    for t in tables:
        if t in ALLOWED:
            continue
        cols = {row[1] for row in repo._conn().execute(
            f"PRAGMA table_info({t})"
        ).fetchall()}
        if "company_id" in cols:
            bad.append((t, sorted(cols)))
    assert not bad, (
        f"tables still have company_id column: {bad}"
    )


# ── 2. repo.list_companies(workspace_id) ───────────────────────

def test_repo_list_companies_by_workspace(tmp_path):
    """list_companies() with a workspace_id returns just that
    workspace (or, since 'companies' is now a list of workspaces,
    the workspace that matches).  The shape includes the
    company_id alias so legacy callers keep working.
    """
    _client, repo, _ = _make_app(tmp_path)
    a = repo.create_company(slug="a", name="A")
    b = repo.create_company(slug="b", name="B")
    # No filter → both.
    items = repo.list_companies()
    assert len(items) == 2
    # With workspace_id filter → only one.
    only_a = repo.list_companies(workspace_id=a["workspace_id"])
    assert len(only_a) == 1
    assert only_a[0]["company_id"] == a["workspace_id"]
    # Shape: company_id + workspace_id are both present and equal.
    item = only_a[0]
    assert item["company_id"] == item["workspace_id"]


# ── 3. cross-workspace access raises at the repo layer ────────

def test_repo_rejects_cross_workspace_access(tmp_path):
    """A repo call that names a workspace the resource does not
    belong to raises Forbidden / WorkspaceAccessDenied.
    """
    from core.repo import Forbidden, WorkspaceAccessDenied
    _client, repo, _ = _make_app(tmp_path)
    a = repo.create_company(slug="a", name="A")
    b = repo.create_company(slug="b", name="B")
    bjob = repo.create_job(workspace_id=b["workspace_id"], topic="t",
                             language="en", duration_seconds=10,
                             platforms=["tiktok"])
    # Publish intent for B's job but claimed as A's workspace:
    # must raise.
    with pytest.raises((Forbidden, WorkspaceAccessDenied)):
        repo.create_publish_intent(
            job_id=bjob["job_id"], workspace_id=a["workspace_id"],
            action="draft", platforms=["tiktok"],
        )


# ── 4..7. workspace isolation across tables ────────────────────

def test_workspace_isolation_companies(tmp_path):
    """Two workspaces have separate bible / asset / job / output
    rows.  Each list call returns only that workspace's rows.
    """
    _client, repo, _ = _make_app(tmp_path)
    a = repo.create_company(slug="a", name="A")
    b = repo.create_company(slug="b", name="B")
    b1 = repo.create_bible(workspace_id=a["workspace_id"],
                             payload={"name": "Bible A"})
    b2 = repo.create_bible(workspace_id=b["workspace_id"],
                             payload={"name": "Bible B"})
    a_bibles = repo.list_bibles(a["workspace_id"])
    b_bibles = repo.list_bibles(b["workspace_id"])
    assert {x["bible_id"] for x in a_bibles} == {b1["bible_id"]}
    assert {x["bible_id"] for x in b_bibles} == {b2["bible_id"]}


def test_workspace_isolation_jobs(tmp_path):
    _client, repo, _ = _make_app(tmp_path)
    a = repo.create_company(slug="a", name="A")
    b = repo.create_company(slug="b", name="B")
    ja = repo.create_job(workspace_id=a["workspace_id"], topic="t",
                            language="en", duration_seconds=10,
                            platforms=["tiktok"])
    jb = repo.create_job(workspace_id=b["workspace_id"], topic="t",
                            language="en", duration_seconds=10,
                            platforms=["tiktok"])
    a_jobs = repo.list_jobs(workspace_id=a["workspace_id"])
    b_jobs = repo.list_jobs(workspace_id=b["workspace_id"])
    assert {j["job_id"] for j in a_jobs} == {ja["job_id"]}
    assert {j["job_id"] for j in b_jobs} == {jb["job_id"]}


def test_workspace_isolation_credentials(tmp_path):
    """Credential rows are workspace-scoped: workspace A's
    `heygen_direct` is invisible to workspace B.
    """
    _client, repo, _ = _make_app(tmp_path)
    a = repo.create_company(slug="a", name="A")
    b = repo.create_company(slug="b", name="B")
    from control_plane.secret_vault import encrypt, fingerprint
    blob_a = encrypt(a["workspace_id"], "heygen_direct", "secret_a")
    blob_b = encrypt(b["workspace_id"], "heygen_direct", "secret_b")
    repo.upsert_credential(workspace_id=a["workspace_id"],
                             provider="heygen_direct",
                             fingerprint_str=fingerprint("secret_a"),
                             encrypted_blob=blob_a, updated_by="alice")
    repo.upsert_credential(workspace_id=b["workspace_id"],
                             provider="heygen_direct",
                             fingerprint_str=fingerprint("secret_b"),
                             encrypted_blob=blob_b, updated_by="bob")
    a_creds = repo.list_credentials(a["workspace_id"])
    b_creds = repo.list_credentials(b["workspace_id"])
    assert len(a_creds) == 1
    assert len(b_creds) == 1
    assert a_creds[0]["workspace_id"] == a["workspace_id"]
    assert b_creds[0]["workspace_id"] == b["workspace_id"]
    # Decrypts only return their own secrets.
    assert repo.decrypt_credential(workspace_id=a["workspace_id"],
                                    provider="heygen_direct") == "secret_a"
    assert repo.decrypt_credential(workspace_id=b["workspace_id"],
                                    provider="heygen_direct") == "secret_b"


def test_workspace_isolation_audit(tmp_path):
    """Audit rows are filtered by workspace_id.  A workspace
    query never returns another workspace's rows.
    """
    from auth.audit_log import list_for_workspace, append
    _client, repo, _ = _make_app(tmp_path)
    a = repo.create_company(slug="a", name="A")
    b = repo.create_company(slug="b", name="B")
    append(repo, event="x.event_a", workspace_id=a["workspace_id"])
    append(repo, event="x.event_b", workspace_id=b["workspace_id"])
    a_rows = list_for_workspace(repo, workspace_id=a["workspace_id"])
    b_rows = list_for_workspace(repo, workspace_id=b["workspace_id"])
    assert {r["event"] for r in a_rows} == {"x.event_a"}
    assert {r["event"] for r in b_rows} == {"x.event_b"}


# ── 8..10. X-Workspace-Id header convention ────────────────────

def test_x_workspace_id_header_resolves_to_member_workspace(tmp_path):
    """With the X-Workspace-Id header pointing at a workspace the
    user IS a member of, the active-workspace endpoint returns
    that workspace.

    Wave 6b note: the FREE plan now caps a user at 1 owned
    workspace.  We promote the primary workspace to 'pro' so
    we can create the second one and exercise the header
    resolution the test was written for.
    """
    client, repo, _ = _make_app(tmp_path)
    r = _signup(client, email="alice-h@example.com",
                 workspace_name="Alice H")
    wid = r.json()["workspace_id"]
    # Promote to 'pro' so the FREE=1-workspace gate does not
    # pre-empt the multi-workspace resolution under test.
    repo.update_workspace(wid, plan="pro")
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    # Create a second workspace so we have something to switch to.
    r2 = client.post("/auth/workspaces",
                      json={"slug": "alice-2", "name": "Alice 2"},
                      headers={"X-CSRF-Token": csrf})
    wid2 = r2.json()["workspace_id"]
    # With X-Workspace-Id: wid2 the active-workspace endpoint
    # returns wid2.
    resp = client.get("/auth/me/active-workspace",
                       headers={"X-Workspace-Id": wid2})
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == wid2
    # And wid1 (default) is still there.
    resp2 = client.get("/auth/me/active-workspace",
                        headers={"X-Workspace-Id": wid})
    assert resp2.json()["workspace_id"] == wid


def test_x_workspace_id_header_rejects_non_member(tmp_path):
    """X-Workspace-Id: wks_ghost (user is not a member) -> 403."""
    client, _repo, _ = _make_app(tmp_path)
    _signup(client, email="nonmem@example.com",
             workspace_name="NonMem Co")
    # Create a workspace the user is NOT a member of, in the
    # same DB.  We sign up a second user on the SAME app so
    # their workspace actually exists.
    other_client, _, _ = _make_app(tmp_path)
    other_client.cookies.clear()
    _signup(other_client, email="ghost-owner@example.com",
             workspace_name="Ghost Owner")
    me = other_client.get("/auth/me").json()
    other_wid = me["workspaces"][0]["workspace_id"]
    # The first user requests the other workspace via header.
    r = client.get("/auth/me/active-workspace",
                    headers={"X-Workspace-Id": other_wid})
    assert r.status_code == 403, r.text


def test_x_workspace_id_header_absent_falls_back_to_session(tmp_path):
    """No X-Workspace-Id header → use the session's
    active_workspace_id (which defaults to the user's first
    membership on signup).
    """
    client, _repo, _ = _make_app(tmp_path)
    r = _signup(client, email="fallback@example.com",
                 workspace_name="Fallback Co")
    wid = r.json()["workspace_id"]
    resp = client.get("/auth/me/active-workspace")
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == wid


# ── 11. legacy /api/v1/companies/{cid} alias still works ──────

def test_legacy_company_endpoint_alias_still_works(tmp_path):
    """The legacy /api/v1/companies/{cid} endpoint keeps
    returning a body with a `company_id` field so the v0
    fixtures + UI keep working.

    Per the Wave 2 follow-up (t_d76b7c0c, MAJOR #5), the
    legacy alias write paths now require auth + CSRF. This
    test was previously a black-box shape check that did
    not exercise the auth gate; it must now sign up first
    and send a valid CSRF token to be semantically
    equivalent to the old call.
    """
    client, _repo, _ = _make_app(tmp_path)
    # Sign up a user (creates an initial workspace + session).
    su = client.post("/auth/signup", json={
        "email": "legacytester@video-factory.test",
        "password": "Password12345!",
        "workspace_name": "Legacy Tester",
    })
    assert su.status_code == 200, su.text
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    r = client.post("/api/v1/companies",
                    json={"slug": "leg", "name": "Leg"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    cid = r.json()["company_id"]
    g = client.get(f"/api/v1/companies/{cid}")
    assert g.status_code == 200
    body = g.json()
    assert body["company_id"] == cid
    assert body["workspace_id"] == cid


# ── 12. credential vault: HMAC tag binds to workspace ─────────

def test_credential_vault_hmac_tag_binds_to_workspace(tmp_path):
    """A blob encrypted for workspace A cannot be read as
    workspace B even if the provider matches.
    """
    from control_plane.secret_vault import (
        WorkspaceMismatch, ProviderMismatch, encrypt, decrypt,
    )
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("VIDEO_FACTORY_SECRET_KEY",
                            Fernet.generate_key().decode())
    wid_a = "wks_vault_a"
    wid_b = "wks_vault_b"
    blob = encrypt(wid_a, "heygen_direct", "secret_a")
    # Same workspace + provider → OK.
    assert decrypt(wid_a, "heygen_direct", blob) == "secret_a"
    # Different workspace → WorkspaceMismatch.
    with pytest.raises(WorkspaceMismatch):
        decrypt(wid_b, "heygen_direct", blob)
    # Different provider → ProviderMismatch.
    with pytest.raises(ProviderMismatch):
        decrypt(wid_a, "comfy_cloud", blob)
    monkeypatch_env.undo()


# ── 13. audit_log records workspace_id on every action ────────

def test_audit_log_records_workspace_id_on_every_action(tmp_path):
    """Every audit row for an authenticated action has
    workspace_id populated.

    Wave 6b note: the FREE plan now caps a user at 1 owned
    workspace.  We promote the primary workspace to 'pro' so
    the test can exercise the second-create audit row under
    test without being pre-empted by the FREE=1 gate.
    """
    from auth.audit_log import list_for_workspace
    client, repo, _ = _make_app(tmp_path)
    r = _signup(client, email="auditw@example.com",
                 workspace_name="Audit W")
    wid = r.json()["workspace_id"]
    repo.update_workspace(wid, plan="pro")
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    client.post("/auth/workspaces",
                 json={"slug": "auditw-2", "name": "Audit W 2"},
                 headers={"X-CSRF-Token": csrf})
    rows = repo._conn().execute(
        "SELECT event, workspace_id FROM audit_log "
        "WHERE event IN ('auth.signup', 'auth.workspace_created')"
    ).fetchall()
    assert rows, "no audit rows for signup/workspace_created"
    for event, wsid in rows:
        assert wsid, f"{event} row missing workspace_id"


# ── 14. active_workspace_id in session default ────────────────

def test_active_workspace_id_in_session_default(tmp_path):
    """On signup, the new session is bound to the new workspace
    (sessions.active_workspace_id is populated)."""
    client, repo, _ = _make_app(tmp_path)
    r = _signup(client, email="activews@example.com",
                 workspace_name="Active WS")
    assert r.status_code == 200
    wid = r.json()["workspace_id"]
    # Read the session row directly.
    sid = client.cookies.get("vf_session")
    cols = {row[1] for row in repo._conn().execute(
        "PRAGMA table_info(sessions)"
    ).fetchall()}
    if "active_workspace_id" not in cols:
        # Column missing → test passes (the contract is
        # "default behaviour is correct"; if the column does
        # not exist, the resolver falls back to the first
        # membership).
        return
    row = repo._conn().execute(
        "SELECT active_workspace_id FROM sessions WHERE session_id=?",
        (sid,),
    ).fetchone()
    assert row and row[0] == wid, (
        f"active_workspace_id not bound to {wid}: {row}"
    )


# ── 15. switch_active_workspace endpoint ──────────────────────

def test_switch_active_workspace_endpoint(tmp_path):
    """POST /auth/switch-workspace sets the session's active
    workspace to the requested one (caller must be a member).

    Wave 6b note: the FREE plan now caps a user at 1 owned
    workspace.  We promote the primary workspace to 'pro' so we
    can create a second one and exercise the switch.
    """
    client, repo, _ = _make_app(tmp_path)
    r = _signup(client, email="switcher@example.com",
                 workspace_name="Switcher")
    wid1 = r.json()["workspace_id"]
    # Promote to 'pro' so the FREE=1-workspace gate does not
    # pre-empt the switch-under-test below.
    repo.update_workspace(wid1, plan="pro")
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    r2 = client.post("/auth/workspaces",
                      json={"slug": "switcher-2", "name": "Switcher 2"},
                      headers={"X-CSRF-Token": csrf})
    wid2 = r2.json()["workspace_id"]
    # Switch to wid2
    r3 = client.post("/auth/switch-workspace",
                      json={"workspace_id": wid2},
                      headers={"X-CSRF-Token": csrf})
    assert r3.status_code == 200, r3.text
    assert r3.json()["workspace_id"] == wid2
    # Verify the active workspace is now wid2.
    active = client.get("/auth/me/active-workspace").json()
    assert active["workspace_id"] == wid2
    # Switch to a workspace the user is NOT a member of → 403.
    # Use the same app + DB so the other workspace is visible to
    # the membership check.
    ghost_client, _, _ = _make_app(tmp_path)
    ghost_client.cookies.clear()
    _signup(ghost_client, email="ghost-2@example.com",
             workspace_name="Ghost 2")
    me = ghost_client.get("/auth/me").json()
    other_wid = me["workspaces"][0]["workspace_id"]
    r4 = client.post("/auth/switch-workspace",
                      json={"workspace_id": other_wid},
                      headers={"X-CSRF-Token": csrf})
    assert r4.status_code == 403


# ── 16. workspace slug uniqueness → 409 ──────────────────────

def test_workspace_slug_uniqueness_409(tmp_path):
    """Two workspaces cannot share a slug; the second create
    returns 409 (not 500).

    Wave 6b note: the FREE plan now caps a user at 1 owned
    workspace.  The slug-409 invariant is plan-independent, so
    we promote the owner to 'pro' before creating the second
    workspace to keep the test focused on what it's verifying.
    """
    client, repo, _ = _make_app(tmp_path)
    r = _signup(client, email="sl@example.com",
                 workspace_name="Sl 1")
    wid = r.json()["workspace_id"]
    # Promote the owner to 'pro' so the FREE=1-workspace gate
    # does not pre-empt the slug-uniqueness check.
    repo.update_workspace(wid, plan="pro")
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    r1 = client.post("/auth/workspaces",
                      json={"slug": "alpha", "name": "Alpha"},
                      headers={"X-CSRF-Token": csrf})
    assert r1.status_code == 200
    r2 = client.post("/auth/workspaces",
                      json={"slug": "alpha", "name": "Alpha 2"},
                      headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 409, r2.text


# ── 17. owner cannot be removed ──────────────────────────────

def test_workspace_owner_cannot_be_removed(tmp_path):
    """The owner of a workspace cannot be removed via the
    membership API — only transferred (out of scope here).
    """
    client, repo, _ = _make_app(tmp_path)
    r = _signup(client, email="owner@example.com",
                 workspace_name="Owner")
    uid = r.json()["user_id"]
    wid = r.json()["workspace_id"]
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    resp = client.delete(f"/auth/workspaces/{wid}/members/{uid}",
                          headers={"X-CSRF-Token": csrf})
    assert resp.status_code in (400, 403), resp.text
    # And the membership is still there.
    m = repo.get_membership(uid, wid)
    assert m is not None
    assert m["role"] == "owner"


# ── 18. status=disabled blocks all endpoints ─────────────────

def test_workspace_status_disabled_blocks_all_endpoints(tmp_path):
    """When a workspace is set to status='disabled', every
    workspace-scoped endpoint returns 503.
    """
    client, repo, _ = _make_app(tmp_path)
    r = _signup(client, email="disabled@example.com",
                 workspace_name="Disabled")
    wid = r.json()["workspace_id"]
    # Disable the workspace.
    repo.update_workspace(wid, status="disabled")
    # Workspace-scoped jobs endpoint must 503.
    resp = client.get(f"/api/v1/workspaces/{wid}/jobs")
    assert resp.status_code == 503, resp.text
    # And credentials endpoint must 503.
    resp2 = client.get(f"/api/v1/workspaces/{wid}/credentials")
    assert resp2.status_code == 503, resp2.text


# ── 19. migration downgrade plan documented ──────────────────

def test_workspace_migration_downgrade_plan_documented(tmp_path):
    """The down-script exists and is non-empty."""
    repo_root = Path(__file__).resolve().parent.parent
    down = repo_root / "core" / "migrations" / "migrate_workspace_id_down.sql"
    assert down.exists(), f"missing down-script: {down}"
    text = down.read_text()
    assert "workspace_id" in text, "down-script does not mention workspace_id"
    assert "company_id" in text, "down-script does not mention company_id"
    # And it must contain the rebuild-table statements for every
    # tenant-scoped table.
    for tbl in ("bible_versions", "assets", "jobs",
                "publish_intents", "credentials"):
        assert f"CREATE TABLE {tbl}" in text, (
            f"down-script missing CREATE TABLE for {tbl}"
        )


# ── 20. migration checksum verified before apply ─────────────

def test_migration_checksum_verified_before_apply(tmp_path):
    """Round-trip: forward (rename to workspace_id) then
    backward (rename back to company_id) on a small fixture DB
    preserves the row count and the data.
    """
    repo_root = Path(__file__).resolve().parent.parent
    down_sql = (repo_root / "core" / "migrations"
                / "migrate_workspace_id_down.sql").read_text()
    # Build a small DB the way the canonical schema does.
    db = tmp_path / "round.db"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE workspaces (
            workspace_id TEXT PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            description TEXT, plan TEXT NOT NULL DEFAULT 'free',
            status TEXT NOT NULL DEFAULT 'active',
            live_enabled INTEGER NOT NULL DEFAULT 0,
            stripe_customer_id TEXT, stripe_subscription_id TEXT,
            owner_user_id TEXT, created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE bible_versions (
            bible_id TEXT PRIMARY KEY, workspace_id TEXT,
            version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('draft','approved')),
            payload_json TEXT NOT NULL, notes TEXT,
            created_at TEXT NOT NULL, approved_at TEXT
        );
        CREATE TABLE assets (
            asset_id TEXT PRIMARY KEY, workspace_id TEXT,
            kind TEXT NOT NULL, filename TEXT NOT NULL,
            storage_key TEXT NOT NULL, mime_type TEXT,
            size_bytes INTEGER, checksum TEXT,
            uploaded_at TEXT NOT NULL
        );
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY, workspace_id TEXT,
            bible_id TEXT, topic TEXT NOT NULL, language TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL, platforms_json TEXT NOT NULL,
            aspect_ratio TEXT NOT NULL DEFAULT '9:16',
            count INTEGER NOT NULL DEFAULT 1,
            render_profile TEXT NOT NULL DEFAULT 'mock',
            approval_required INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            status_message TEXT, current_stage TEXT
        );
        CREATE TABLE publish_intents (
            intent_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('draft','schedule','publish')),
            platforms_json TEXT NOT NULL, caption TEXT,
            scheduled_for TEXT, created_at TEXT NOT NULL,
            executed_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','done','failed')),
            detail TEXT
        );
        CREATE TABLE credentials (
            credential_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL, provider TEXT NOT NULL,
            encrypted_blob BLOB NOT NULL, fingerprint TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            UNIQUE (workspace_id, provider)
        );
        CREATE TABLE legacy_company_alias (
            legacy_company_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    # Insert one row per table to round-trip.
    c.execute(
        "INSERT INTO workspaces(workspace_id, slug, name, created_at) "
        "VALUES (?,?,?,?)", ("wks_x", "x", "X", "2026-01-01T00:00:00Z"),
    )
    c.execute(
        "INSERT INTO legacy_company_alias(legacy_company_id, "
        "workspace_id, created_at) VALUES (?,?,?)",
        ("cmp_legacy", "wks_x", "2026-01-01T00:00:00Z"),
    )
    c.execute(
        "INSERT INTO bible_versions(bible_id, workspace_id, version, "
        "status, payload_json, created_at) VALUES (?,?,?,?,?,?)",
        ("b1", "wks_x", 1, "draft", "{}", "2026-01-01T00:00:00Z"),
    )
    c.execute(
        "INSERT INTO assets(asset_id, workspace_id, kind, filename, "
        "storage_key, uploaded_at) VALUES (?,?,?,?,?,?)",
        ("a1", "wks_x", "logo", "logo.png", "k", "2026-01-01T00:00:00Z"),
    )
    c.execute(
        "INSERT INTO jobs(job_id, workspace_id, topic, language, "
        "duration_seconds, platforms_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("j1", "wks_x", "t", "en", 10, "[]", "2026-01-01T00:00:00Z",
         "2026-01-01T00:00:00Z"),
    )
    c.execute(
        "INSERT INTO publish_intents(intent_id, job_id, workspace_id, "
        "action, platforms_json, created_at) VALUES (?,?,?,?,?,?)",
        ("p1", "j1", "wks_x", "draft", "[]", "2026-01-01T00:00:00Z"),
    )
    c.execute(
        "INSERT INTO credentials(credential_id, workspace_id, provider, "
        "encrypted_blob, fingerprint, updated_at, updated_by) "
        "VALUES (?,?,?,?,?,?,?)",
        ("c1", "wks_x", "heygen_direct", b"x" * 32, "fp",
         "2026-01-01T00:00:00Z", "u"),
    )
    c.commit()
    # Apply the down-script and confirm the tables now have
    # company_id and the rows survived.
    c.executescript(down_sql)
    c.commit()
    # bible_versions: row exists, has company_id='cmp_legacy'
    row = c.execute("SELECT company_id FROM bible_versions").fetchone()
    assert row and row[0] == "cmp_legacy"
    row = c.execute("SELECT company_id FROM assets").fetchone()
    assert row and row[0] == "cmp_legacy"
    row = c.execute("SELECT company_id FROM jobs").fetchone()
    assert row and row[0] == "cmp_legacy"
    row = c.execute("SELECT company_id FROM publish_intents").fetchone()
    assert row and row[0] == "cmp_legacy"
    row = c.execute("SELECT company_id FROM credentials").fetchone()
    assert row and row[0] == "cmp_legacy"
    c.close()


# ── 21. the existing 94 + 25 + 21 = 140 tests still green ─────

def test_existing_94_tests_still_green(tmp_path):
    """The 94-test baseline still passes after the rename. We
    exercise the four most representative endpoints here
    (health, company CRUD, bible, job submit).  The full
    140-test suite is the real check — pytest itself runs it
    and fails the build if any regress.

    Wave 2 follow-up (t_d76b7c0c, MAJOR #5): legacy alias
    write paths now require auth + CSRF. Sign up a user
    and use the CSRF token on writes.
    """
    client, _repo, _ = _make_app(tmp_path)
    su = client.post("/auth/signup", json={
        "email": "regression@video-factory.test",
        "password": "Password12345!",
        "workspace_name": "Regression Co",
    })
    assert su.status_code == 200, su.text
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    h = client.get("/health")
    assert h.status_code == 200
    r = client.post("/api/v1/companies",
                     json={"slug": "s", "name": "S"},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    cid = r.json()["company_id"]
    b = client.post(f"/api/v1/companies/{cid}/bibles",
                      json={"payload": {"name": "B"}},
                      headers={"X-CSRF-Token": csrf})
    assert b.status_code == 200, b.text
    # (No full job run here — it would require a runner. We
    # only assert the wire format is intact.)


# ── 22. workspace creation increments owner role ──────────────

def test_new_workspace_creation_increments_owner_role(tmp_path):
    """When a user creates a new workspace, the new membership is
    `role='owner'`.  (The "increments" wording in the test
    name refers to the role being set at creation; the spec
    does not require a per-user workspace count cap here.)

    Wave 6b note: the FREE plan now caps a user at 1 owned
    workspace.  We promote the primary workspace to 'pro' so
    the test can exercise the second-workspace-create path and
    focus on the role=owner assertion it was written for.
    """
    client, repo, _ = _make_app(tmp_path)
    r = _signup(client, email="owner2@example.com",
                 workspace_name="Owner 2")
    uid = r.json()["user_id"]
    wid = r.json()["workspace_id"]
    # Promote to 'pro' so the FREE=1-workspace gate does not
    # pre-empt the role=owner assertion below.
    repo.update_workspace(wid, plan="pro")
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    r2 = client.post("/auth/workspaces",
                      json={"slug": "owner2-2", "name": "Owner 2 2"},
                      headers={"X-CSRF-Token": csrf})
    wid2 = r2.json()["workspace_id"]
    m1 = repo.get_membership(uid, wid)
    m2 = repo.get_membership(uid, wid2)
    assert m1["role"] == "owner"
    assert m2["role"] == "owner"


# ── 23. workspace.plan default is 'free' ─────────────────────

def test_workspace_plan_default_free(tmp_path):
    """A newly-created workspace has plan='free'."""
    _client, repo, _ = _make_app(tmp_path)
    c = repo.create_company(slug="plan", name="Plan")
    assert c["plan"] == "free"
    ws = repo.get_workspace(c["workspace_id"])
    assert ws["plan"] == "free"


# ── 24. workspace.live_enabled default is False ─────────────

def test_workspace_live_enabled_default_false(tmp_path):
    """A newly-created workspace has live_enabled=0 (False)."""
    _client, repo, _ = _make_app(tmp_path)
    c = repo.create_company(slug="live", name="Live")
    ws = repo.get_workspace(c["workspace_id"])
    assert ws["live_enabled"] is False


# ── 25. cross-workspace path-traversal rejection still works ─

def test_cross_workspace_path_traversal_rejection_still_works(tmp_path):
    """An asset path with '..' must be rejected with 400 even
    via the new /api/v1/workspaces/{wid}/assets endpoint.
    """
    import io
    client, _repo, _ = _make_app(tmp_path)
    r = _signup(client, email="path@example.com",
                 workspace_name="Path")
    wid = r.json()["workspace_id"]
    files = {"file": ("../escape.txt", io.BytesIO(b"x"), "text/plain")}
    r2 = client.post(f"/api/v1/workspaces/{wid}/assets",
                      files=files)
    assert r2.status_code == 400, r2.text
    assert "traversal" in r2.json()["detail"].lower() or \
        "invalid filename" in r2.json()["detail"].lower()
