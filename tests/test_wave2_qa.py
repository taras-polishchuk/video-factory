"""Wave 2 QA adversarial tests — independent verifier (t_15377ab3).

These cover paths NOT in the developer's suite:
  - Bootstrap-from-legacy-DB (the path the prod DB follows on first
    Wave-2 startup).
  - Six named attack vectors from the QA brief.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from core.repo import Repo
from tests.test_api import env as env  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _set_session_secret(monkeypatch):
    """Auth endpoints 503 without VIDEO_FACTORY_SESSION_SECRET."""
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET",
                       "x" * 64)


def _signup(client, *, email: str, workspace: str) -> dict:
    r = client.post("/auth/signup",
                    json={"email": email,
                          "password": "Strong-pwd-1234",
                          "workspace_name": workspace})
    assert r.status_code == 200, r.text
    return r.json()


def _get_active(client) -> dict:
    r = client.get("/auth/me/active-workspace")
    assert r.status_code == 200, r.text
    return r.json()


# ── Helper: build a pre-Wave-2 legacy schema (companies + company_id) ──


def _write_legacy_db(db) -> None:
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE workspaces (
        workspace_id TEXT PRIMARY KEY,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        plan TEXT NOT NULL DEFAULT 'free',
        status TEXT NOT NULL DEFAULT 'active',
        live_enabled INTEGER NOT NULL DEFAULT 0,
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT,
        owner_user_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE TABLE companies (
        company_id TEXT PRIMARY KEY,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE TABLE users (
        user_id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_login_at TEXT,
        email_verified INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active'
    );
    CREATE TABLE memberships (
        user_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, workspace_id)
    );
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        active_workspace_id TEXT,
        csrf_token TEXT
    );
    CREATE TABLE email_verifications (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE password_resets (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE login_attempts (
        attempt_id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        ip TEXT,
        succeeded INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE audit_log (
        audit_id TEXT PRIMARY KEY,
        event TEXT NOT NULL,
        context_json TEXT,
        created_at TEXT NOT NULL,
        user_id TEXT,
        workspace_id TEXT
    );
    CREATE TABLE bible_versions (
        bible_id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        notes TEXT,
        created_at TEXT NOT NULL,
        approved_at TEXT
    );
    CREATE TABLE assets (
        asset_id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        filename TEXT NOT NULL,
        storage_key TEXT NOT NULL,
        mime_type TEXT,
        size_bytes INTEGER,
        checksum TEXT,
        uploaded_at TEXT NOT NULL
    );
    CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL,
        bible_id TEXT,
        topic TEXT NOT NULL,
        language TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        platforms_json TEXT NOT NULL,
        aspect_ratio TEXT NOT NULL DEFAULT '9:16',
        count INTEGER NOT NULL DEFAULT 1,
        render_profile TEXT NOT NULL DEFAULT 'mock',
        approval_required INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued',
        status_message TEXT,
        current_stage TEXT
    );
    CREATE TABLE job_stages (
        stage_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        stage_key TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        duration_ms INTEGER,
        summary_json TEXT,
        error TEXT,
        ordinal INTEGER NOT NULL
    );
    CREATE TABLE job_outputs (
        output_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        storage_key TEXT NOT NULL,
        filename TEXT NOT NULL,
        mime_type TEXT,
        size_bytes INTEGER,
        checksum TEXT,
        is_mock INTEGER NOT NULL DEFAULT 0,
        kind TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE publish_intents (
        intent_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        action TEXT NOT NULL,
        platforms_json TEXT NOT NULL,
        caption TEXT,
        scheduled_for TEXT,
        created_at TEXT NOT NULL,
        executed_at TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        detail TEXT
    );
    CREATE TABLE integrations (
        name TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT 'mock',
        detail TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE credentials (
        credential_id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        encrypted_blob BLOB NOT NULL,
        fingerprint TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL
    );
    """)
    c.commit()
    c.close()


# ── Check 3: bootstrap from legacy DB ────────────────────────────────


def test_bootstrap_from_legacy_db_succeeds(tmp_path):
    """A real legacy-schema DB must boot into the post-migration schema.

    This is the path the production database follows on first Wave-2
    startup. Today's code crashes with 'no such column: workspace_id'
    because SCHEMA indexes on workspace_id run BEFORE the rename
    migration in _apply_migration_005.
    """
    db = tmp_path / "legacy.db"
    _write_legacy_db(db)
    Repo(str(db))
    c = sqlite3.connect(db)
    for tbl in ("jobs", "assets", "bible_versions",
                "credentials", "publish_intents"):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})")}
        assert "workspace_id" in cols, f"{tbl} missing workspace_id"
        assert "company_id" not in cols, f"{tbl} still has company_id"


# ── Check 5: path traversal rejected ─────────────────────────────────


def test_workspace_id_path_traversal_rejected(env):
    """Path traversal in workspace_id must NOT leak data or 500."""
    client, repo, _ = env
    _signup(client, email="a@example.com", workspace="Alpha")
    for bad in [
        "..%2Fother",
        "..%2F..%2Fetc",
        "wks_AAAA%2F%2E%2E",
        "not-a-workspace",
    ]:
        r = client.get(f"/api/v1/workspaces/{bad}/jobs")
        assert r.status_code in (400, 401, 403, 404), \
            f"unexpected {r.status_code} on workspace_id={bad!r}: {r.text}"
        assert r.status_code != 500


# ── Check 6: legacy /api/v1/companies/<cid> alias still 200 ───────────


def test_legacy_companies_alias_still_returns_200(env):
    """The legacy /api/v1/companies/<cid> alias must return 200.

    Per the Wave 2 follow-up (t_d76b7c0c, MAJOR #5), write
    paths require auth + CSRF. The test was previously a
    black-box shape check that did not exercise the auth
    gate; it must now send a valid CSRF token to be
    semantically equivalent to the old call.
    """
    client, repo, _ = env
    _signup(client, email="owner@example.com", workspace="Acme")
    wid = _get_active(client)["workspace_id"]
    # Pull a CSRF token bound to the session cookie.
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    r = client.post("/api/v1/companies",
                    json={"slug": "acme-1",
                          "name": "Acme One",
                          "workspace_id": wid},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    cid = r.json()["company_id"]
    r = client.get(f"/api/v1/companies/{cid}")
    assert r.status_code == 200, r.text
    assert r.json()["company_id"] == cid


# ── Check 4: cross-workspace attack via session manipulation ────────


def test_cross_workspace_attack_via_session_manipulation(env):
    """Attacker tries to read B's data using A's session.
    Three attack vectors exercised; all must be rejected.
    """
    client, repo, _ = env
    _signup(client, email="alice@example.com", workspace="Alice WS")
    alice_wid = _get_active(client)["workspace_id"]

    c2 = TestClient(client.app)
    _signup(c2, email="bob@example.com", workspace="Bob WS")
    bob_wid = _get_active(c2)["workspace_id"]
    assert alice_wid != bob_wid

    # Bob uses Alice's URL → must be 403.
    r = c2.get(f"/api/v1/workspaces/{alice_wid}/jobs")
    assert r.status_code == 403, r.text

    # Bob forges X-Workspace-Id header → must be 403.
    r = c2.get("/auth/me/active-workspace",
               headers={"X-Workspace-Id": alice_wid})
    assert r.status_code == 403, r.text

    # Alice tampers with her own session's active_workspace_id → bob's.
    # The resolver must NOT return bob's wid for Alice.
    # `set_session_active_workspace` adds the column lazily if absent.
    from auth.workspace_resolver import set_session_active_workspace
    from auth.session import SESSION_COOKIE_NAME
    sid = client.cookies.get(SESSION_COOKIE_NAME)
    assert sid
    set_session_active_workspace(repo, sid, bob_wid)
    r = client.get("/auth/me/active-workspace")
    if r.status_code == 200:
        assert r.json()["workspace_id"] == alice_wid
    else:
        assert r.status_code in (401, 403, 404), r.text


# ── Check 4b: X-Workspace-Id header spoofing ────────────────────────


def test_cross_workspace_attack_via_header_spoofing(env):
    """X-Workspace-Id header must be validated against membership."""
    client, repo, _ = env
    _signup(client, email="carol@example.com", workspace="Carol WS")
    r = client.get("/auth/me/active-workspace",
                   headers={"X-Workspace-Id": "wks_nonexistent"})
    assert r.status_code in (403, 404), r.text


# ── Check 5b: URL-path wid takes precedence over header ─────────────


def test_workspace_id_in_url_path_takes_precedence_over_header(env):
    """URL path wid is authoritative; header cannot override it."""
    client, repo, _ = env
    _signup(client, email="dave@example.com", workspace="Dave WS")
    dave_wid = _get_active(client)["workspace_id"]
    r = client.get(f"/api/v1/workspaces/{dave_wid}/jobs",
                   headers={"X-Workspace-Id": "wks_hacker"})
    assert r.status_code == 200, r.text


# ── Check 5c: workspace_id must be a workspace-shaped token ──


def test_workspace_id_must_be_uuid_or_int_via_real_attack(env):
    """Various malformed wid shapes must be rejected (not 500)."""
    client, repo, _ = env
    _signup(client, email="eve@example.com", workspace="Eve WS")
    for bad in ["' OR 1=1--", "wks_; DROP TABLE jobs;--", "%00"]:
        r = client.get(f"/api/v1/workspaces/{bad}/jobs")
        assert r.status_code in (400, 401, 403, 404), \
            f"unexpected {r.status_code} on bad wid={bad!r}"
        assert r.status_code != 500


# ── Check 5d: migration idempotent (run twice on same DB) ───────────


def test_migration_idempotent_run_twice(tmp_path):
    """Booting Repo() twice on the same DB must not error and must
    leave the schema in the post-migration state."""
    db = tmp_path / "vf.db"
    Repo(str(db))
    Repo(str(db))
    c = sqlite3.connect(db)
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    assert "workspace_id" in cols
    assert "company_id" not in cols


# ── Check 7: disabled workspace blocks even owner ───────────────────


def test_workspace_status_disabled_blocks_even_owner(env):
    """A disabled workspace must reject ALL requests from its owner."""
    client, repo, _ = env
    _signup(client, email="frank@example.com", workspace="Frank WS")
    wid = _get_active(client)["workspace_id"]
    repo._conn().execute(
        "UPDATE workspaces SET status='disabled' WHERE workspace_id=?",
        (wid,),
    )
    repo._conn().commit()
    r = client.get(f"/api/v1/workspaces/{wid}/jobs")
    assert r.status_code in (403, 503), r.text
    r = client.get(f"/api/v1/workspaces/{wid}/credentials")
    assert r.status_code in (403, 503), r.text


# ── Check 7b: vault HMAC binds to workspace_id ─────────────────────


def test_credential_vault_hmac_tag_binds_to_workspace(env):
    """A blob encrypted under workspace A must not decrypt under B
    even with the same Fernet key."""
    client, repo, _ = env
    os.environ["VIDEO_FACTORY_SECRET_KEY"] = Fernet.generate_key().decode()
    from control_plane.secret_vault import encrypt, decrypt, WorkspaceMismatch
    blob = encrypt("wks_A", "heygen_direct", "secret_alpha")
    with pytest.raises(WorkspaceMismatch):
        decrypt("wks_B", "heygen_direct", blob)
    assert decrypt("wks_A", "heygen_direct", blob) == "secret_alpha"


# ── Additional Wave 2 QA findings ────────────────────────────────────


def test_legacy_companies_endpoints_unauthenticated(env):
    """Legacy /api/v1/companies/* endpoints do not require authentication.

    Anonymous users get 200 from GET /api/v1/companies (enumeration)
    and 404 from /api/v1/companies/{cid} (existence oracle). Per
    Wave 2 brief every workspace-scoped endpoint must enforce
    auth + membership.
    """
    client, repo, _ = env
    # Unauthenticated GET list → 200 (info leak, lists all workspaces).
    r = client.get("/api/v1/companies")
    # Note: anonymous call to an auth-gated endpoint must be 401.
    assert r.status_code == 401, (
        f"legacy /api/v1/companies is unauthenticated (got {r.status_code}): "
        f"{r.text}"
    )


def test_legacy_companies_id_get_unauthenticated(env):
    """GET /api/v1/companies/<id> must require auth."""
    client, repo, _ = env
    r = client.get("/api/v1/companies/wks_anything")
    assert r.status_code == 401, (
        f"legacy /api/v1/companies/<id> is unauthenticated "
        f"(got {r.status_code}): {r.text}"
    )


# ── Check 1b: actual production DB boots into Wave 2 ─────────────


def test_production_db_boots_into_wave2():
    """The real production DB at artifacts/video_factory.db must
    boot into Wave 2. This is the single most important
    regression test for the BLOCKER #1 fix (t_d76b7c0c).

    We use a copy so this test does not mutate the live DB
    between runs. The copy is recreated from the .bak
    snapshot on every test invocation.
    """
    import shutil
    repo_root = Path(__file__).resolve().parent.parent
    bak = repo_root / "artifacts" / "video_factory.db.bak"
    if not bak.exists():
        pytest.skip("no artifacts/video_factory.db.bak snapshot found")
    tmp = Path(tempfile.mkstemp(suffix=".db")[1])
    try:
        shutil.copy2(bak, tmp)
        # Open the legacy prod DB. The fix in _bootstrap_schema
        # (reorder: 005 first, then SCHEMA) must let it boot.
        r = Repo(str(tmp))
        # Verify every tenant-scoped table ended up with
        # workspace_id and no company_id.
        c = sqlite3.connect(str(tmp))
        for tbl in ("bible_versions", "assets", "jobs",
                    "publish_intents", "credentials"):
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})")}
            assert "workspace_id" in cols, f"{tbl} missing workspace_id"
            assert "company_id" not in cols, f"{tbl} still has company_id"
        # Verify the companies table was dropped.
        assert not any(
            r[0] == "companies" for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ), "legacy `companies` table should be dropped"
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def test_legacy_companies_id_patch_unauthenticated(env):
    """PATCH /api/v1/companies/<id> must require auth."""
    client, repo, _ = env
    # Bypass the auto-authed wrapper: this test asserts the
    # PATCH is rejected when NO session cookie is present.
    r = client.raw.patch("/api/v1/companies/wks_anything",
                         json={"name": "hijacked"})
    assert r.status_code == 401, (
        f"legacy /api/v1/companies/<id> PATCH is unauthenticated "
        f"(got {r.status_code}): {r.text}"
    )