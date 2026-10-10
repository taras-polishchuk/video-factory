"""SQLite-backed persistence for multi-tenant Video Factory.

Tables (canonical names, Wave 2+):
- workspaces: the multi-tenant root.  `workspace_id` is the FK target
  for every other table.
- bible_versions: Company Video Bible with draft/approved states
- assets: brand assets (logo, fonts, examples)
- jobs: video jobs
- job_stages: per-stage execution record
- job_outputs: per-asset output metadata
- publish_intents: mock draft/schedule/publish records
- integrations: provider connection state (mock-only here)
- credentials: per-workspace encrypted provider secrets (the
  "credential vault"). Plaintext never enters the DB.
- audit_log: cross-actor audit trail
- users + memberships + sessions + email_verifications + password_resets:
  Wave 1 auth foundations.

All FKs scoped to a workspace. Cross-workspace access raises
`WorkspaceAccessDenied` (HTTP 403 at the API layer).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional


# Schema for the Wave-2 canonical layout.  All tenant-scoped tables
# use `workspace_id` and FK into `workspaces(workspace_id)`.
SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    plan TEXT NOT NULL DEFAULT 'free'
        CHECK (plan IN ('free', 'pro', 'enterprise')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended', 'disabled')),
    live_enabled INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    owner_user_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON workspaces(owner_user_id);

CREATE TABLE IF NOT EXISTS bible_versions (
    bible_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','approved')),
    payload_json TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE INDEX IF NOT EXISTS idx_bible_workspace ON bible_versions(workspace_id);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER,
    checksum TEXT,
    uploaded_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE INDEX IF NOT EXISTS idx_assets_workspace ON assets(workspace_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
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
    current_stage TEXT,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id),
    FOREIGN KEY (bible_id) REFERENCES bible_versions(bible_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_workspace ON jobs(workspace_id);

CREATE TABLE IF NOT EXISTS job_stages (
    stage_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed','skipped')),
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    summary_json TEXT,
    error TEXT,
    ordinal INTEGER NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_stages_job ON job_stages(job_id);

CREATE TABLE IF NOT EXISTS job_outputs (
    output_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER,
    checksum TEXT,
    is_mock INTEGER NOT NULL DEFAULT 0,
    kind TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_outputs_job ON job_outputs(job_id);

CREATE TABLE IF NOT EXISTS publish_intents (
    intent_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('draft','schedule','publish')),
    platforms_json TEXT NOT NULL,
    caption TEXT,
    scheduled_for TEXT,
    created_at TEXT NOT NULL,
    executed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','failed')),
    detail TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE INDEX IF NOT EXISTS idx_publish_workspace ON publish_intents(workspace_id);

CREATE TABLE IF NOT EXISTS integrations (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'mock',
    detail TEXT,
    updated_at TEXT NOT NULL
);

-- Per-workspace encrypted credentials (F-credential-vault).
-- One row per (workspace_id, provider_name). The plaintext is never
-- stored; only an encrypted blob. The repo exposes only masked
-- surfaces to the API.
CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    encrypted_blob BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id),
    UNIQUE (workspace_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_credentials_workspace ON credentials(workspace_id);
"""


# Backwards-compat SCHEMA for the OLD layout (pre-Wave-2 test fixtures
# that called `Repo()` against a DB created by an earlier code path).
# Wave 2 drops the `companies` table outright, but old DBs that still
# have one will have it ignored by the new code (only the
# `workspace_id` column matters now).
LEGACY_COMPANIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    company_id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Forbidden(Exception):
    """Legacy alias for WorkspaceAccessDenied.

    Kept so existing routes that raise/catch Forbidden keep working
    through the Wave 2 transition.
    """
    pass


class WorkspaceAccessDenied(Forbidden):
    """Raised when a caller tries to access data outside their workspace.

    The API layer maps this to HTTP 403.
    """
    pass


class NotFound(Exception):
    pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _compute_checksum(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# Process-local fallback for _bootstrap_lock when an OS-level lock is
# unavailable (":memory:" DBs, non-POSIX hosts, filesystems without
# flock support). Keyed by the resolved DB directory.
_BOOTSTRAP_FALLBACK_LOCKS: dict = {}
_BOOTSTRAP_FALLBACK_GUARD = threading.Lock()


@contextmanager
def _bootstrap_lock(db_path: str) -> Iterator[None]:
    """Exclusive cross-process + cross-thread lock for schema bootstrap.

    Uses ``flock`` on the DB's *directory* (no sidecar lock file to
    litter, and never an fd on the DB file itself: closing a second fd
    on the DB file would drop SQLite's own POSIX locks). Each call opens
    its own fd, so threads of one process exclude each other too.
    Falls back to a process-local lock when flock is not available.
    """
    directory = None
    if db_path and db_path != ":memory:" and not db_path.startswith("file:"):
        directory = str(Path(db_path).resolve().parent)
    fd = None
    if directory is not None:
        try:
            import fcntl
            fd = os.open(directory, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            fd = None
    if fd is not None:
        try:
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return
    key = directory or db_path
    with _BOOTSTRAP_FALLBACK_GUARD:
        lock = _BOOTSTRAP_FALLBACK_LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


class Repo:
    """Thread-safe SQLite wrapper. Per-thread connections guarded by a lock."""

    def __init__(self, db_path: str, *,
                 db_key: Optional[str] = None) -> None:
        """Construct a Repo.

        `db_key` overrides the env var `VIDEO_FACTORY_DB_KEY`. If
        the env var is set the bare constructor picks it up
        automatically; tests can pass `db_key="x"` directly.

        Wave 5 startup rules:

      * if `VIDEO_FACTORY_DB_KEY` is set AND the file at `db_path`
        exists AND it is plaintext → raise `PlaintextDBRefused`.
        The operator must run a one-time migration via
        `core.cipher_db.migrate_plaintext_to_cipher`.  We do NOT
        silently re-open a "keyed" deployment as plaintext, because
        that defeats the entire point of Wave 5.

      * if `VIDEO_FACTORY_DB_KEY` is set AND the file doesn't
        exist → create a new cipher DB at `db_path` with the key.

      * if `VIDEO_FACTORY_DB_KEY` is unset → behave exactly as in
        Wave 1..4 (plaintext dev / legacy).
        """
        self.db_path = db_path
        # Effective key (env override wins unless caller forced one).
        import core.cipher_db as _cipher
        if db_key is None:
            db_key = _cipher._cipher_key()  # type: ignore[attr-defined]
        self.db_key = db_key
        # If a key is configured, the file MUST be cipher (or
        # missing). Reject plaintext.
        if self.db_key is not None:
            if _cipher.detect_plaintext(db_path):  # type: ignore[attr-defined]
                raise _cipher.PlaintextDBRefused(  # type: ignore[attr-defined]
                    f"{db_path} is a plaintext SQLite DB but "
                    f"VIDEO_FACTORY_DB_KEY is set. Refusing to open. "
                    f"Run one of:\n"
                    f"  python -c 'from core.cipher_db import "
                    f"migrate_plaintext_to_cipher; "
                    f"migrate_plaintext_to_cipher(plain_path=\"{db_path}\","
                    f" cipher_path=\"{db_path}\", "
                    f"key=...)'\n"
                    f"or unset VIDEO_FACTORY_DB_KEY for plaintext "
                    f"dev mode (NOT for production)."
                )
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Open one connection per thread (sqlite3 is connection-thread-local).
        import threading as _t
        self._local = _t.local()
        # ── Cross-thread epoch counter (Wave 5 follow-up-2) ──
        # `Repo.close()` only clears the *calling thread's* thread-local
        # connection.  After a master-key rotation, every thread that
        # had previously called `repo._conn()` still holds a
        # connection that was PRAGMA-key'd with the OLD key, even
        # though `repo.db_key` has been updated and the cipher file on
        # disk is the NEW one.  That stale connection either:
        #   * raises RotationInProgress forever (because it reads
        #     the pre-swap `key_rotation` row whose
        #     `rotation_in_progress` is still 1 from its OWN conn's
        #     perspective — the post-swap UPDATE used a different
        #     conn on the rotating thread), OR
        #   * silently encrypts writes with the OLD key into the
        #     NEW-cipher file (unreadable to any NEW-key reader).
        # Bumping `self._epoch` after the swap forces every thread's
        # next `_conn()` access to close its stale conn and reopen
        # with the current `self.db_key`.  Cheaper than walking
        # `threading.enumerate()` to enumerate per-thread caches and
        # avoids the need for a global connection registry.
        self._epoch: int = 0
        self._bootstrap_schema()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        c_epoch = getattr(self._local, "conn_epoch", None)
        # If the per-thread conn is from an earlier epoch (i.e. the
        # Repo was rotated after this thread last opened its conn),
        # discard it.  A fresh open with `self.db_key` is the only
        # sqlcipher3-documented way to switch the page-encryption key
        # on a connection — re-issuing PRAGMA key on an existing
        # connection is not officially supported and behaves
        # inconsistently across sqlcipher3 versions.
        if c is not None and c_epoch != self._epoch:
            try:
                c.close()
            except Exception:
                pass
            c = None
            self._local.conn = None
        if c is None:
            import core.cipher_db as _cipher
            c = _cipher.connect(self.db_path, key=self.db_key)
            # SQLCipher raises a private `DatabaseError` subclass
            # that does NOT inherit from `sqlite3.DatabaseError`
            # (verified 2026-10).  The rest of the Repo (and tests)
            # expect stdlib `sqlite3.DatabaseError` for "wrong key
            # / corrupt page" cases, so we normalise the failure
            # of the post-open pragmas here.  We don't swallow the
            # error — we re-raise it as a stdlib exception so the
            # caller's `except sqlite3.DatabaseError` still fires.
            try:
                c.execute("PRAGMA journal_mode=WAL")
            except Exception as exc:
                try:
                    c.close()
                except Exception:
                    pass
                if isinstance(exc, sqlite3.DatabaseError):
                    raise
                raise sqlite3.DatabaseError(
                    f"sqlcipher post-open pragma failed: {exc}"
                ) from exc
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
            self._local.conn_epoch = self._epoch
        return c

    def _bootstrap_schema(self):
        # F-11: two processes (or threads) opening the same DB at the
        # same time must not interleave the schema bootstrap below.
        # Several steps are check-then-ALTER (PRAGMA table_info, then
        # ALTER TABLE ADD COLUMN); interleaved, the second booter
        # crashes with "duplicate column name". The migration ledger
        # itself is guarded by BEGIN IMMEDIATE; this lock covers the
        # rest of the bootstrap.
        with _bootstrap_lock(self.db_path):
            self._bootstrap_schema_locked()

    def _bootstrap_schema_locked(self):
        c = self._conn()
        # ── Wave 2 BLOCKER fix (t_d76b7c0c) ──
        # The order of operations matters when the DB already
        # has the legacy schema (a real prod DB carries
        # `company_id` columns + a `companies` table from before
        # Wave 2). The previous order ran SCHEMA's
        # `CREATE INDEX ... ON workspace_id` BEFORE the
        # company_id -> workspace_id rename, raising
        # "no such column: workspace_id".
        #
        # The correct order is:
        #   1. Apply the rename (005) so legacy columns
        #      are now `workspace_id`.
        #   2. Run SCHEMA so the indexes that reference
        #      `workspace_id` find the column.
        #   3. Apply remaining migrations (idempotent).
        #   4. Extend audit_log.
        #   5. Run SCHEMA once more (rebuilds indexes after
        #      005's table-rebuild drops and recreates).
        self._apply_migration_005()
        c.executescript(SCHEMA)
        # Wave 1: apply auth migrations idempotently.
        from core.migrations import apply_migrations
        apply_migrations(c)
        # Extend audit_log with user_id + workspace_id + actor if absent.
        self._extend_audit_log_columns()
        # Wave 6a: ensure workspaces has the Stripe subscription
        # period-end column. Idempotent — PRAGMA table_info check
        # inside the helper skips ALTER if already present.
        self._extend_workspaces_billing_columns()
        # Run the canonical schema again so anything that the
        # Wave-2 rename removed is recreated with workspace_id FKs.
        c.executescript(SCHEMA)
        for name in ("heygen", "comfy_cloud", "comfy_gpu_worker", "n8n", "publer"):
            c.execute(
                "INSERT OR IGNORE INTO integrations(name, enabled, mode, detail, updated_at) VALUES (?,?,?,?,?)",
                (name, 0, "mock", "default mock mode", now_iso()),
            )
        # F3: append runner_audit table for idempotency-mirror events.
        from control_plane.repo_helpers import RUNNER_AUDIT_SCHEMA, bootstrap_runner_audit
        c.executescript(RUNNER_AUDIT_SCHEMA)
        bootstrap_runner_audit(self)

    # ── Migration helpers ─────────────────────────────────────

    def _table_columns(self, table: str) -> set[str]:
        """Return the column names of a table (lowercased)."""
        rows = self._conn().execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
        return {r[1].lower() for r in rows}

    def _table_exists(self, table: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    def _extend_audit_log_columns(self) -> None:
        """Ensure audit_log has user_id + workspace_id + actor columns.

        SQLite has no IF NOT EXISTS for ADD COLUMN prior to 3.35, so
        we check via PRAGMA table_info and add missing columns
        manually. Idempotent.
        """
        c = self._conn()
        cols = {row[1] for row in c.execute("PRAGMA table_info(audit_log)").fetchall()}
        if "user_id" not in cols:
            c.execute("ALTER TABLE audit_log ADD COLUMN user_id TEXT")
        if "workspace_id" not in cols:
            c.execute("ALTER TABLE audit_log ADD COLUMN workspace_id TEXT")
        if "actor" not in cols:
            c.execute("ALTER TABLE audit_log ADD COLUMN actor TEXT")
        # Keyset pagination of GET /workspaces/{wid}/audit (MAJ-4).
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_workspace_created "
            "ON audit_log(workspace_id, created_at)"
        )
        c.commit()

    def _extend_workspaces_billing_columns(self) -> None:
        """Ensure workspaces has the Wave 6a billing columns.

        SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
        before 3.35, so we check via `PRAGMA table_info` and ALTER
        if missing.  Idempotent — safe to call on every bootstrap.

        Adds:

        - ``current_period_end INTEGER`` — unix timestamp of the
          Stripe subscription's current period end. NULL means no
          active subscription.
        """
        c = self._conn()
        cols = {row[1].lower()
                for row in c.execute("PRAGMA table_info(workspaces)").fetchall()}
        if "current_period_end" not in cols:
            c.execute(
                "ALTER TABLE workspaces ADD COLUMN current_period_end INTEGER"
            )
            c.commit()

    def _apply_migration_005(self) -> None:
        """Idempotent Wave-2 migration: company_id -> workspace_id.

        Steps (each guarded so a re-run is a no-op):

        1. If a `companies` table exists, backfill each row into
           `workspaces` (creating a stub workspace with no owner).
           Track the old company_id -> new workspace_id mapping in
           `legacy_company_alias` so the legacy /api/v1/companies
           alias can still resolve.
        2. For each tenant-scoped table (bible_versions, assets,
           jobs, publish_intents, credentials), if the column is
           `company_id` and `workspace_id` is missing, add
           `workspace_id`. Backfill from `company_id` (joined via
           the alias table). Drop the `company_id` column.
        3. Drop the `companies` table at the end (after alias
           mapping is captured).
        """
        c = self._conn()
        # 0b. Idempotent: ensure legacy `workspaces` table has
        # the columns the migration needs to insert into.
        # Pre-Wave-2 workspaces tables may lack `description`
        # and/or `updated_at`. The migration's INSERT statement
        # references them. (Wave 2 BLOCKER fix — t_d76b7c0c.)
        if self._table_exists("workspaces"):
            ws_cols = self._table_columns("workspaces")
            if "description" not in ws_cols:
                c.execute("ALTER TABLE workspaces "
                          "ADD COLUMN description TEXT")
            if "updated_at" not in ws_cols:
                c.execute("ALTER TABLE workspaces "
                          "ADD COLUMN updated_at TEXT")
        # 0c. Idempotent: ensure legacy `workspaces.owner_user_id`
        # is nullable. Pre-Wave-2 `workspaces` declared it NOT
        # NULL; the migration creates stub workspaces from
        # `companies` rows that have no associated user. The
        # new SCHEMA declares it nullable, so we relax the
        # constraint here for legacy DBs. (Wave 2 BLOCKER fix.)
        if self._table_exists("workspaces"):
            ws_cols_info = c.execute(
                "PRAGMA table_info(workspaces)"
            ).fetchall()
            owner_notnull = any(
                r[1] == "owner_user_id" and r[3] == 1
                for r in ws_cols_info
            )
            if owner_notnull:
                # Standard table-rebuild pattern to relax NOT NULL.
                rows = c.execute("SELECT * FROM workspaces").fetchall()
                old_cols = [r[1] for r in ws_cols_info]
                c.execute("ALTER TABLE workspaces "
                          "RENAME TO workspaces__legacy")
                c.execute(
                    "CREATE TABLE workspaces ("
                    "workspace_id TEXT PRIMARY KEY, "
                    "slug TEXT UNIQUE NOT NULL, "
                    "name TEXT NOT NULL, "
                    "description TEXT, "
                    "plan TEXT NOT NULL DEFAULT 'free' "
                    "    CHECK (plan IN ('free', 'pro', 'enterprise')), "
                    "status TEXT NOT NULL DEFAULT 'active' "
                    "    CHECK (status IN ('active', 'suspended', "
                    "                    'disabled', 'deleted')), "
                    "live_enabled INTEGER NOT NULL DEFAULT 0, "
                    "stripe_customer_id TEXT, "
                    "stripe_subscription_id TEXT, "
                    "owner_user_id TEXT, "
                    "created_at TEXT NOT NULL, "
                    "updated_at TEXT, "
                    "FOREIGN KEY (owner_user_id) REFERENCES users(user_id))"
                )
                new_cols = [r[1] for r in c.execute(
                    "PRAGMA table_info(workspaces)"
                ).fetchall()]
                placeholders = ",".join("?" for _ in new_cols)
                insert_sql = (
                    f"INSERT INTO workspaces "
                    f"({','.join(new_cols)}) VALUES ({placeholders})"
                )
                new_idx = {c: i for i, c in enumerate(new_cols)}
                legacy_idx = {c: i for i, c in enumerate(old_cols)}
                for r in rows:
                    new_row = [None] * len(new_cols)
                    for col, idx in legacy_idx.items():
                        if col in new_idx:
                            new_row[new_idx[col]] = r[idx]
                    c.execute(insert_sql, tuple(new_row))
                c.execute("DROP TABLE workspaces__legacy")
        # 0d. Idempotent: ensure a system user exists for stub
        # workspaces migrated from `companies`. The new SCHEMA
        # declares `owner_user_id` nullable, so we no longer
        # need a non-null owner — but to keep audit trails
        # useful we still create the system user.
        sys_uid = new_id("usr")
        try:
            c.execute(
                "INSERT INTO users(user_id, email, password_hash, "
                "status, email_verified, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (sys_uid, "system@video-factory.internal",
                 "!system-no-login", "active", 1, now_iso()),
            )
        except Exception:
            # Already exists — fine.
            pass
        # 0e. Idempotent: ensure every legacy tenant-scoped table
        # has a `workspace_id` column (added with default NULL).
        # Step 2 below updates these to point at the new
        # workspace rows; if the column doesn't exist yet the
        # UPDATE silently fails inside its try/except. (Wave 2
        # BLOCKER fix — t_d76b7c0c.)
        for tbl in ("bible_versions", "assets", "jobs",
                    "publish_intents", "credentials"):
            if not self._table_exists(tbl):
                continue
            cols = self._table_columns(tbl)
            if "workspace_id" not in cols and "company_id" in cols:
                c.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN workspace_id TEXT"
                )
        # 0. ensure the alias table exists.
        c.execute(
            "CREATE TABLE IF NOT EXISTS legacy_company_alias ("
            "  legacy_company_id TEXT PRIMARY KEY,"
            "  workspace_id TEXT NOT NULL,"
            "  created_at TEXT NOT NULL"
            ")"
        )
        # 0a. Idempotent on legacy `login_attempts` tables that
        # still have a `created_at` column (vault mission era).
        # Migration 002 references `attempted_at`; rename the
        # legacy column here so the SCHEMA + migration 002
        # indexes succeed on a real prod DB. (Wave 2 BLOCKER
        # fix — t_d76b7c0c.)
        if self._table_exists("login_attempts"):
            la_cols = self._table_columns("login_attempts")
            if "created_at" in la_cols and "attempted_at" not in la_cols:
                # Use the standard SQLite table-rebuild pattern
                # to rename the column (SQLite < 3.35 has no
                # RENAME COLUMN).
                rows = c.execute("SELECT * FROM login_attempts").fetchall()
                old_cols = [r[1] for r in c.execute(
                    "PRAGMA table_info(login_attempts)"
                ).fetchall()]
                c.execute("ALTER TABLE login_attempts "
                          "RENAME TO login_attempts__legacy")
                c.execute(
                    "CREATE TABLE login_attempts ("
                    "attempt_id TEXT PRIMARY KEY, "
                    "ip TEXT NOT NULL, "
                    "email TEXT NOT NULL, "
                    "succeeded INTEGER NOT NULL DEFAULT 0, "
                    "attempted_at TEXT NOT NULL)"
                )
                new_cols = [r[1] for r in c.execute(
                    "PRAGMA table_info(login_attempts)"
                ).fetchall()]
                placeholders = ",".join("?" for _ in new_cols)
                insert_sql = (
                    f"INSERT INTO login_attempts "
                    f"({','.join(new_cols)}) VALUES ({placeholders})"
                )
                col_map = [
                    "attempted_at" if col == "created_at" else col
                    for col in old_cols
                ]
                new_idx = {c: i for i, c in enumerate(new_cols)}
                legacy_idx = {c: i for i, c in enumerate(old_cols)}
                for r in rows:
                    new_row = [None] * len(new_cols)
                    for col, idx in legacy_idx.items():
                        target = col_map[idx]
                        if target in new_idx:
                            new_row[new_idx[target]] = r[idx]
                    c.execute(insert_sql, tuple(new_row))
                c.execute("DROP TABLE login_attempts__legacy")
        # 1. (alias table already created above.)
        # 2. If companies exists, migrate each row -> workspace, store alias.
        if self._table_exists("companies"):
            cols = self._table_columns("companies")
            if "company_id" in cols and "slug" in cols:
                rows = c.execute(
                    "SELECT company_id, slug, name, description, "
                    "created_at, updated_at FROM companies"
                ).fetchall()
                t = now_iso()
                for r in rows:
                    cid, slug, name, desc, created_at, updated_at = r
                    # Skip if we've already aliased this cid.
                    existing = c.execute(
                        "SELECT workspace_id FROM legacy_company_alias "
                        "WHERE legacy_company_id=?",
                        (cid,),
                    ).fetchone()
                    if existing:
                        continue
                    # Create a workspace that mirrors the company.
                    new_wid = new_id("wks")
                    try:
                        c.execute(
                            "INSERT INTO workspaces(workspace_id, slug, name, "
                            "description, plan, status, live_enabled, "
                            "created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (new_wid, slug, name, desc or "",
                             "free", "active", 0, created_at, updated_at),
                        )
                    except Exception:
                        # Slug collision: suffix and retry.
                        new_slug = f"{slug}-migrated-{uuid.uuid4().hex[:6]}"
                        c.execute(
                            "INSERT INTO workspaces(workspace_id, slug, name, "
                            "description, plan, status, live_enabled, "
                            "created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (new_wid, new_slug, name, desc or "",
                             "free", "active", 0, created_at, updated_at),
                        )
                    c.execute(
                        "INSERT INTO legacy_company_alias("
                        "legacy_company_id, workspace_id, created_at) "
                        "VALUES (?,?,?)",
                        (cid, new_wid, t),
                    )
                    # Backfill workspace_id for rows pointing at this cid.
                    for tbl in ("bible_versions", "assets", "jobs",
                                "publish_intents", "credentials"):
                        if not self._table_exists(tbl):
                            continue
                        tbl_cols = self._table_columns(tbl)
                        if "company_id" in tbl_cols:
                            try:
                                c.execute(
                                    f"UPDATE {tbl} SET workspace_id=? "
                                    f"WHERE company_id=? AND (workspace_id IS NULL OR workspace_id='')",
                                    (new_wid, cid),
                                )
                            except Exception:
                                pass
        # 3. Drop `company_id` from each legacy table if present,
        #    ensure `workspace_id` is present.
        # Disable foreign-key checks during the rebuild:
        # after `ALTER TABLE x RENAME TO x__legacy`, any FK from
        # another table that points at `x` now points at
        # `x__legacy`. The new `x` table is created with fresh
        # FKs but the other tables' FKs still bind to the
        # legacy name. The DROP at the end of each iteration
        # would fail on that. We disable FKs, do all the
        # rebuilds, then re-enable + verify.
        c.execute("PRAGMA foreign_keys=OFF")
        for tbl in ("bible_versions", "assets", "jobs",
                    "publish_intents", "credentials"):
            if not self._table_exists(tbl):
                continue
            cols = self._table_columns(tbl)
            if "company_id" in cols:
                # SQLite < 3.35 cannot DROP COLUMN.  We use the
                # standard "rebuild table" pattern: rename -> create new
                # -> copy -> drop old.
                if "workspace_id" not in cols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN workspace_id TEXT")
                # Copy the rows.
                rows = c.execute(f"SELECT * FROM {tbl}").fetchall()
                # Get column names of the live table.
                live_cols = [r[1] for r in c.execute(
                    f"PRAGMA table_info({tbl})"
                ).fetchall()]
                # Rebuild.
                c.execute(f"ALTER TABLE {tbl} RENAME TO {tbl}__legacy")
                create_sql = ""
                if tbl == "bible_versions":
                    create_sql = (
                        "CREATE TABLE bible_versions ("
                        "bible_id TEXT PRIMARY KEY, workspace_id TEXT, "
                        "version INTEGER NOT NULL, status TEXT NOT NULL "
                        "CHECK (status IN ('draft','approved')), "
                        "payload_json TEXT NOT NULL, notes TEXT, "
                        "created_at TEXT NOT NULL, approved_at TEXT)"
                    )
                elif tbl == "assets":
                    create_sql = (
                        "CREATE TABLE assets ("
                        "asset_id TEXT PRIMARY KEY, workspace_id TEXT, "
                        "kind TEXT NOT NULL, filename TEXT NOT NULL, "
                        "storage_key TEXT NOT NULL, mime_type TEXT, "
                        "size_bytes INTEGER, checksum TEXT, "
                        "uploaded_at TEXT NOT NULL)"
                    )
                elif tbl == "jobs":
                    create_sql = (
                        "CREATE TABLE jobs ("
                        "job_id TEXT PRIMARY KEY, workspace_id TEXT, "
                        "bible_id TEXT, topic TEXT NOT NULL, "
                        "language TEXT NOT NULL, duration_seconds INTEGER NOT NULL, "
                        "platforms_json TEXT NOT NULL, "
                        "aspect_ratio TEXT NOT NULL DEFAULT '9:16', "
                        "count INTEGER NOT NULL DEFAULT 1, "
                        "render_profile TEXT NOT NULL DEFAULT 'mock', "
                        "approval_required INTEGER NOT NULL DEFAULT 0, "
                        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                        "state TEXT NOT NULL DEFAULT 'queued', "
                        "status_message TEXT, current_stage TEXT)"
                    )
                elif tbl == "publish_intents":
                    create_sql = (
                        "CREATE TABLE publish_intents ("
                        "intent_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
                        "workspace_id TEXT NOT NULL, "
                        "action TEXT NOT NULL "
                        "CHECK (action IN ('draft','schedule','publish')), "
                        "platforms_json TEXT NOT NULL, caption TEXT, "
                        "scheduled_for TEXT, created_at TEXT NOT NULL, "
                        "executed_at TEXT, "
                        "status TEXT NOT NULL DEFAULT 'pending' "
                        "CHECK (status IN ('pending','done','failed')), "
                        "detail TEXT)"
                    )
                elif tbl == "credentials":
                    create_sql = (
                        "CREATE TABLE credentials ("
                        "credential_id TEXT PRIMARY KEY, "
                        "workspace_id TEXT NOT NULL, provider TEXT NOT NULL, "
                        "encrypted_blob BLOB NOT NULL, "
                        "fingerprint TEXT NOT NULL, "
                        "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, "
                        "UNIQUE (workspace_id, provider))"
                    )
                c.execute(create_sql)
                # Copy rows from the legacy table, mapping columns.
                if rows:
                    legacy_cols = [r[1] for r in c.execute(
                        f"PRAGMA table_info({tbl}__legacy)"
                    ).fetchall()]
                    # If the legacy column was named company_id, map to
                    # workspace_id in the new table.
                    col_map = []
                    for col in legacy_cols:
                        if col == "company_id":
                            col_map.append("workspace_id")
                        else:
                            col_map.append(col)
                    # Build a SELECT that maps legacy_cols -> new cols.
                    select_exprs = []
                    for i, col in enumerate(legacy_cols):
                        if col == "company_id":
                            select_exprs.append("workspace_id")
                        else:
                            select_exprs.append(col)
                    # We rely on the fact that the rows are in
                    # legacy_cols order.  Build a new tuple per row in
                    # the new schema's column order.
                    new_col_list = [r[1] for r in c.execute(
                        f"PRAGMA table_info({tbl})"
                    ).fetchall()]
                    placeholders = ",".join("?" for _ in new_col_list)
                    insert_sql = f"INSERT INTO {tbl} ({','.join(new_col_list)}) VALUES ({placeholders})"
                    new_idx = {c: i for i, c in enumerate(new_col_list)}
                    legacy_idx = {c: i for i, c in enumerate(legacy_cols)}
                    for r in rows:
                        new_row = [None] * len(new_col_list)
                        for col, idx in legacy_idx.items():
                            target = col_map[idx]
                            if target in new_idx:
                                new_row[new_idx[target]] = r[idx]
                        c.execute(insert_sql, tuple(new_row))
                c.execute(f"DROP TABLE {tbl}__legacy")
        # 4. Drop `companies` table at the end.
        if self._table_exists("companies"):
            c.execute("DROP TABLE companies")
        # 5. Rebuild `memberships` so its `FOREIGN KEY (workspace_id)`
        #    points at the new `workspaces` table. The earlier
        #    `workspaces` rebuild (0c) renamed the table to
        #    `workspaces__legacy` and dropped it; SQLite preserves FK
        #    metadata through `ALTER TABLE RENAME`, so the existing
        #    `memberships` table (created on a legacy DB by the
        #    Wave-1 auth migration in `core/migrations/__init__.py`)
        #    still claims its FK targets `workspaces__legacy`.
        #    Result: any `INSERT INTO memberships (...)` against a
        #    legacy prod DB raises `sqlite3.OperationalError:
        #    no such table: main.workspaces__legacy` at
        #    `api/auth.py:158` (Wave 3 follow-up — t_7e0153fd).
        #
        #    The SCHEMA in `core/repo.py` does not declare
        #    `memberships` (it lives in `core/migrations/__init__.py`
        #    migration 001), so a fresh DB has no `memberships` table
        #    at this point — `CREATE TABLE IF NOT EXISTS` in the
        #    migration runs AFTER 005 and creates it correctly.
        #    On a legacy prod DB, the table exists but its FK is bad;
        #    rebuild it now. Idempotent: no-op when the FK is fine.
        self._rebuild_memberships_workspace_fk()
        # Re-enable foreign-key checks and verify integrity.
        c.execute("PRAGMA foreign_keys=ON")
        try:
            c.execute("PRAGMA foreign_key_check")
        except Exception as e:
            import sys
            print(f"005: foreign_key_check failed: {e}", file=sys.stderr)
        c.commit()

    def _rebuild_memberships_workspace_fk(self) -> None:
        """Idempotent: rebuild `memberships` if its FK still points
        at the dropped `workspaces__legacy` table.

        Called from `_apply_migration_005` AFTER `DROP TABLE
        workspaces__legacy` and BEFORE `PRAGMA foreign_keys=ON`
        (so the rebuild can safely drop the broken table without
        tripping FK checks against the missing legacy name).

        Behaviour:

        - `memberships` does not exist (fresh DB at migration 005
          time): no-op. The Wave-1 auth migration in
          `core/migrations/__init__.py` runs AFTER 005 and creates
          `memberships` with the correct FK from the start.
        - `memberships` exists and its `workspace_id` FK targets
          something other than `workspaces__legacy`: no-op.
        - `memberships` exists and its `workspace_id` FK targets
          `workspaces__legacy` (the dropped temp name): rebuild
          the table using the standard SQLite
          rename -> create new -> copy -> drop legacy pattern,
          so the FK points at the new `workspaces` table.
        """
        c = self._conn()
        if not self._table_exists("memberships"):
            return  # fresh DB path — nothing to rebuild.
        fk_rows = c.execute(
            "PRAGMA foreign_key_list(memberships)"
        ).fetchall()
        # fk_rows columns: id, seq, table, from, to, on_update, on_delete, match
        bad = any(row[2] == "workspaces__legacy" for row in fk_rows)
        if not bad:
            return  # already fine.
        rows = c.execute("SELECT * FROM memberships").fetchall()
        old_cols = [
            r[1] for r in c.execute(
                "PRAGMA table_info(memberships)"
            ).fetchall()
        ]
        c.execute(
            "ALTER TABLE memberships RENAME TO memberships__legacy"
        )
        # Recreate with the canonical Wave-1 schema + a corrected
        # workspace_id FK pointing at the new `workspaces` table.
        # The role CHECK matches migration 001 exactly.
        c.execute(
            "CREATE TABLE memberships ("
            "user_id TEXT NOT NULL, "
            "workspace_id TEXT NOT NULL, "
            "role TEXT NOT NULL "
            "    CHECK (role IN ('owner','admin','editor','viewer')), "
            "created_at TEXT NOT NULL, "
            "PRIMARY KEY (user_id, workspace_id), "
            "FOREIGN KEY (user_id) REFERENCES users(user_id), "
            "FOREIGN KEY (workspace_id) REFERENCES "
            "    workspaces(workspace_id))"
        )
        new_cols = [
            r[1] for r in c.execute(
                "PRAGMA table_info(memberships)"
            ).fetchall()
        ]
        new_idx = {col: i for i, col in enumerate(new_cols)}
        legacy_idx = {col: i for i, col in enumerate(old_cols)}
        placeholders = ",".join("?" for _ in new_cols)
        insert_sql = (
            f"INSERT INTO memberships ({','.join(new_cols)}) "
            f"VALUES ({placeholders})"
        )
        for r in rows:
            new_row = [None] * len(new_cols)
            for col, idx in legacy_idx.items():
                if col in new_idx:
                    new_row[new_idx[col]] = r[idx]
            c.execute(insert_sql, tuple(new_row))
        c.execute("DROP TABLE memberships__legacy")
        # Re-create the index that the legacy table carried. SCHEMA
        # in core/repo.py doesn't declare `memberships` so the
        # second `c.executescript(SCHEMA)` later in bootstrap
        # doesn't recreate it; `core/migrations/__init__.py`
        # migration 001 does declare it, but a legacy DB has
        # already run that migration so the `CREATE INDEX IF NOT
        # EXISTS` is a no-op once `memberships__legacy` is the
        # table it bound to. Recreate here so the new `memberships`
        # has its workspace_id index again.
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_memberships_workspace "
            "ON memberships(workspace_id)"
        )

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def readiness_probe(self, *, busy_timeout_ms: int = 100) -> Optional[str]:
        """Cheap real-DB check for ``/readyz`` (Wave 8 QA-N2).

        Returns ``None`` when the DB can serve reads and writes, or a
        fixed reason token:

        * ``db_file_missing``: the file at ``db_path`` is gone. Open
          connections keep working on the unlinked inode, so every
          write since is lost at the next restart.
        * ``db_file_replaced``: a different file now sits at
          ``db_path`` than the one this thread's connection opened
          (deleted and recreated underneath the process).
        * ``db_schema_missing``: the file has no schema (a fresh
          connection opened a recreated, empty file).
        * ``db_locked``: another connection holds the write lock for
          longer than ``busy_timeout_ms``; real writes would block
          for the full busy timeout and then fail.

        Any other failure raises (``/readyz`` reports the exception
        class). Uses this thread's cached connection, lowers its busy
        timeout for the probe only and restores it, and never writes:
        ``BEGIN IMMEDIATE`` takes the write lock, ``ROLLBACK`` drops it.
        Call it off the event loop (it can wait ``busy_timeout_ms``).
        """
        try:
            st = os.stat(self.db_path)
        except FileNotFoundError:
            return "db_file_missing"
        ident = (st.st_dev, st.st_ino)
        c = self._conn()
        # Remember which file this thread's connection was opened on.
        # A new connection (first use, Repo.close(), key-rotation
        # epoch bump) re-arms the check.
        if getattr(self._local, "probe_conn", None) is not c:
            self._local.probe_conn = c
            self._local.probe_ident = ident
        elif self._local.probe_ident != ident:
            return "db_file_replaced"
        if c.in_transaction:
            # This thread is inside its own transaction; a nested
            # BEGIN would fail for the wrong reason. Reads still work.
            c.execute("SELECT 1").fetchone()
            return None
        prev = c.execute("PRAGMA busy_timeout").fetchone()[0]
        c.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        try:
            row = c.execute(
                "SELECT count(*) FROM sqlite_master").fetchone()
            if not row or not row[0]:
                return "db_schema_missing"
            try:
                c.execute("BEGIN IMMEDIATE")
            except Exception as exc:  # noqa: BLE001 - sqlcipher3 has its own classes
                msg = str(exc).lower()
                if "locked" in msg or "busy" in msg:
                    return "db_locked"
                raise
            c.execute("ROLLBACK")
            return None
        finally:
            c.execute(f"PRAGMA busy_timeout={int(prev)}")

    @contextmanager
    def tx(self, *, _skip_rotation_check: bool = False
           ) -> Iterator[sqlite3.Connection]:
        """Open a transaction with rotation-in-progress gating.

        Wave 5 follow-up (t_abf264b4): while a master-key rotation
        is in progress, every write transaction MUST raise
        `RotationInProgress` so concurrent writers cannot land on
        a freshly-swapped cipher file with a stale key.  The
        canonical write path in the API layer is `with repo.tx() as
        c: c.execute(...)`, so gating here covers all the methods
        on `Repo` that perform INSERT/UPDATE/DELETE.

        Internal callers (e.g. `key_rotation.rotate` itself) pass
        `_skip_rotation_check=True` so the rotation function can
        write its own bookkeeping rows while the flag is set.

        Reads via `repo._conn()` (no lock, no rotation check) are
        unchanged; they observe the on-disk state of the live DB.
        """
        with self._lock:
            if not _skip_rotation_check:
                # Imported lazily so the Repo module remains
                # importable without the key_rotation dependency
                # being available at import time.
                from control_plane import key_rotation as _kr
                if not _kr.writes_allowed(self):
                    raise _kr.RotationInProgress(
                        "master-key rotation in progress; "
                        "writes are paused"
                    )
            c = self._conn()
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise

    @contextmanager
    def immediate_tx(self) -> Iterator[sqlite3.Connection]:
        """A real write transaction: ``BEGIN IMMEDIATE`` ... ``COMMIT``.

        Connections are opened with ``isolation_level=None``
        (autocommit), so ``tx()`` above does NOT group statements: each
        execute commits on its own. Read-then-write invariants (job
        quota, budget) need an actual transaction. ``BEGIN IMMEDIATE``
        takes SQLite's RESERVED lock up front, which serialises the
        whole block against every other writer: other threads of this
        process (they own other connections) and other processes.
        The SELECTs inside the block therefore see the latest committed
        state and nobody can insert in between. ``self._lock`` keeps the
        in-process ordering identical to ``tx()``. Rotation-gated like
        ``tx()``.
        """
        with self._lock:
            from control_plane import key_rotation as _kr
            if not _kr.writes_allowed(self):
                raise _kr.RotationInProgress(
                    "master-key rotation in progress; writes are paused"
                )
            c = self._conn()
            if c.in_transaction:
                # A best-effort writer on this thread left a statement
                # transaction open; never nest into it.
                c.execute("COMMIT")
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise

    # ── Workspace access guard ────────────────────────────────

    def require_workspace(self, workspace_id: str) -> dict:
        """Verify the workspace exists AND is not disabled.

        Raises WorkspaceAccessDenied(403) for missing or
        disabled workspaces. Returns the row on success.
        """
        ws = self.get_workspace(workspace_id)
        if ws.get("status") in ("disabled", "suspended", "deleted"):
            raise WorkspaceAccessDenied(
                f"workspace {workspace_id} is {ws['status']}; access denied"
            )
        return ws

    @staticmethod
    def _resolve_wid(company_id: Optional[str],
                        workspace_id: Optional[str]) -> str:
        """Accept either `company_id` (legacy) or `workspace_id`
        (canonical) and return the canonical workspace_id.

        Raises ValueError if neither or both are passed.
        """
        if company_id is not None and workspace_id is not None:
            if company_id == workspace_id:
                return workspace_id
            raise ValueError(
                "pass either company_id or workspace_id, not both"
            )
        if workspace_id is not None:
            return workspace_id
        if company_id is not None:
            return company_id
        raise ValueError("workspace_id (or company_id) is required")

    # ── Workspaces (canonical tenant) ──────────────────────────

    def create_workspace(self, *, slug: str, name: str,
                            owner_user_id: Optional[str] = None,
                            plan: str = "free",
                            description: str = "") -> dict:
        workspace_id = new_id("wks")
        t = now_iso()
        if plan not in ("free", "pro", "enterprise"):
            raise ValueError(f"invalid plan: {plan!r}")
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO workspaces(workspace_id, slug, name, "
                    "description, plan, status, live_enabled, "
                    "owner_user_id, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (workspace_id, slug.lower(), name, description,
                     plan, "active", 0, owner_user_id, t, t),
                )
                # Auto-add owner membership if we know the owner.
                if owner_user_id:
                    c.execute(
                        "INSERT OR REPLACE INTO memberships(user_id, "
                        "workspace_id, role, created_at) "
                        "VALUES (?,?,?,?)",
                        (owner_user_id, workspace_id, "owner", t),
                    )
        except Exception as e:
            if "UNIQUE constraint failed" in str(e) and "slug" in str(e):
                raise ValueError(
                    f"workspace slug {slug!r} already exists"
                ) from e
            raise
        return self.get_workspace(workspace_id)

    def get_workspace(self, workspace_id: str) -> dict:
        row = self._conn().execute(
            "SELECT workspace_id, slug, name, description, plan, status, "
            "live_enabled, owner_user_id, stripe_customer_id, "
            "stripe_subscription_id, current_period_end, "
            "created_at, updated_at "
            "FROM workspaces WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"workspace {workspace_id} not found")
        return self._row_workspace_full(row)

    def get_workspace_by_slug(self, slug: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT workspace_id, slug, name, description, plan, status, "
            "live_enabled, owner_user_id, stripe_customer_id, "
            "stripe_subscription_id, current_period_end, "
            "created_at, updated_at "
            "FROM workspaces WHERE slug=?",
            (slug.lower(),),
        ).fetchone()
        if not row:
            return None
        return self._row_workspace_full(row)

    def list_workspaces(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT workspace_id, slug, name, description, plan, status, "
            "live_enabled, owner_user_id, stripe_customer_id, "
            "stripe_subscription_id, current_period_end, "
            "created_at, updated_at "
            "FROM workspaces ORDER BY created_at, rowid"
        ).fetchall()
        return [self._row_workspace_full(r) for r in rows]

    def update_workspace(self, workspace_id: str, *,
                            name: Optional[str] = None,
                            description: Optional[str] = None,
                            plan: Optional[str] = None,
                            status: Optional[str] = None,
                            live_enabled: Optional[bool] = None) -> dict:
        with self.tx() as c:
            existing = self.get_workspace(workspace_id)
            new_name = name if name is not None else existing["name"]
            new_desc = description if description is not None else existing.get("description", "")
            new_plan = plan if plan is not None else existing["plan"]
            new_status = status if status is not None else existing["status"]
            new_live = live_enabled if live_enabled is not None else existing.get("live_enabled", False)
            if new_plan not in ("free", "pro", "enterprise"):
                raise ValueError(f"invalid plan: {new_plan!r}")
            if new_status not in ("active", "suspended", "disabled"):
                raise ValueError(f"invalid status: {new_status!r}")
            c.execute(
                "UPDATE workspaces SET name=?, description=?, plan=?, "
                "status=?, live_enabled=?, updated_at=? "
                "WHERE workspace_id=?",
                (new_name, new_desc, new_plan, new_status,
                 1 if new_live else 0, now_iso(), workspace_id),
            )
        return self.get_workspace(workspace_id)

    def _row_workspace_full(self, row) -> dict:
        return {
            "workspace_id": row[0],
            "slug": row[1],
            "name": row[2],
            "description": row[3] or "",
            "plan": row[4],
            "status": row[5],
            "live_enabled": bool(row[6]),
            "owner_user_id": row[7],
            "stripe_customer_id": row[8],
            "stripe_subscription_id": row[9],
            "current_period_end": row[10],
            "created_at": row[11],
            "updated_at": row[12],
        }

    # Legacy alias: the old `_row_workspace` is still referenced by
    # the auth router and Wave 1 tests.  Keep it as a thin wrapper
    # that returns the same shape with the canonical workspace_id key.
    def _row_workspace(self, row) -> dict:
        return self._row_workspace_full(row)

    # ── Wave 6a billing helpers ─────────────────────────────

    def get_workspace_billing(self, workspace_id: str) -> dict:
        """Return the Stripe billing state for a workspace.

        Returns ``{}`` (NOT a NotFound) when no row matches — the
        API layer uses this to decide whether to surface a
        'no customer yet, click Upgrade' empty state.  Only the
        three Stripe columns are returned; full workspace state is
        available via ``get_workspace``.
        """
        row = self._conn().execute(
            "SELECT stripe_customer_id, stripe_subscription_id, "
            "current_period_end FROM workspaces WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if not row:
            return {}
        return {
            "workspace_id": workspace_id,
            "stripe_customer_id": row[0],
            "stripe_subscription_id": row[1],
            "current_period_end": row[2],
        }

    def upsert_stripe_customer(self, workspace_id: str,
                                 customer_id: str) -> dict:
        """Persist a Stripe customer id for a workspace.

        Called immediately after the API creates the Customer
        object (or after a successful Checkout Session whose
        `customer` field was filled).  Returns the refreshed
        workspace billing row.
        """
        if not customer_id:
            raise ValueError("customer_id required")
        with self.tx() as c:
            existing = self.get_workspace(workspace_id)
            c.execute(
                "UPDATE workspaces SET stripe_customer_id=?, "
                "updated_at=? WHERE workspace_id=?",
                (customer_id, now_iso(), workspace_id),
            )
        _ = existing  # touched only to raise NotFound early on bad id
        return self.get_workspace_billing(workspace_id)

    def upsert_stripe_subscription(self, workspace_id: str,
                                     subscription_id: str,
                                     period_end: Optional[int]) -> dict:
        """Persist a Stripe subscription id + current_period_end.

        `period_end` is the unix timestamp from the Stripe
        subscription object (``current_period_end`` field).  Stored
        as INTEGER; NULL means no active subscription.
        """
        if not subscription_id:
            raise ValueError("subscription_id required")
        with self.tx() as c:
            existing = self.get_workspace(workspace_id)
            c.execute(
                "UPDATE workspaces SET stripe_subscription_id=?, "
                "current_period_end=?, updated_at=? WHERE workspace_id=?",
                (subscription_id, period_end, now_iso(), workspace_id),
            )
        _ = existing
        return self.get_workspace_billing(workspace_id)

    def update_workspace_billing(self, workspace_id: str,
                                   *,
                                   plan: Optional[str] = None,
                                   stripe_customer_id: Optional[str] = None,
                                   stripe_subscription_id: Optional[str] = None,
                                   current_period_end: Optional[int] = None,
                                   clear_subscription: bool = False) -> dict:
        """Multi-column setter for Stripe billing state.

        Only the fields that are not None are updated; ``None``
        means 'leave unchanged'.  ``clear_subscription=True``
        forcibly nulls ``stripe_subscription_id`` (used by the
        ``customer.subscription.deleted`` webhook to express the
        "ended" state without ambiguity).  This is the unified
        entry point used by the Wave 6c webhook handlers (plan
        flip + period refresh + customer binding all happen
        here).  Returns the refreshed billing row via
        ``get_workspace_billing``.
        """
        sets = []
        vals: list = []
        if plan is not None:
            sets.append("plan=?")
            vals.append(plan)
        if stripe_customer_id is not None:
            sets.append("stripe_customer_id=?")
            vals.append(stripe_customer_id)
        if clear_subscription:
            sets.append("stripe_subscription_id=NULL")
        elif stripe_subscription_id is not None:
            sets.append("stripe_subscription_id=?")
            vals.append(stripe_subscription_id)
        if current_period_end is not None:
            sets.append("current_period_end=?")
            vals.append(current_period_end)
        if not sets:
            return self.get_workspace_billing(workspace_id)
        sets.append("updated_at=?")
        vals.append(now_iso())
        vals.append(workspace_id)
        # Confirm workspace exists so callers get a clear
        # NotFound rather than a silent no-op.
        self.get_workspace(workspace_id)
        with self.tx() as c:
            c.execute(
                f"UPDATE workspaces SET {', '.join(sets)} "
                f"WHERE workspace_id=?",
                tuple(vals),
            )
        return self.get_workspace_billing(workspace_id)

    def insert_stripe_event(self, *, event_id: str, event_type: str,
                              raw_json: str) -> bool:
        """Try to record a Stripe event for idempotency.

        Returns True if the event was newly recorded (caller
        should proceed to process it). Returns False if the
        event id was already present (caller should treat as
        duplicate and skip processing).  Race-safe via the
        PRIMARY KEY on stripe_events.id — concurrent inserts
        with the same id will see one True and the rest False.
        """
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO stripe_events(id, type, received_at, "
                    "raw_json) VALUES (?,?,?,?)",
                    (event_id, event_type, int(time.time()), raw_json),
                )
            return True
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return False
            raise

    def mark_stripe_event_processed(self, event_id: str) -> None:
        """Stamp processed_at on a previously-recorded event.

        Called after the handler successfully applied the event
        to the workspace. Idempotent — re-stamping a processed
        event is a no-op.
        """
        with self.tx() as c:
            c.execute(
                "UPDATE stripe_events SET processed_at=? "
                "WHERE id=? AND processed_at IS NULL",
                (int(time.time()), event_id),
            )

    def release_stripe_event(self, event_id: str) -> bool:
        """Drop the idempotency claim of an event that FAILED processing.

        ``insert_stripe_event`` claims the id before the handler runs.
        If the handler then raises, Stripe retries the same event id;
        without releasing the claim the retry is acked as a duplicate
        and the billing change is lost.  Only rows that were never
        stamped processed are removed, so a completed event can never
        be re-opened.  Returns True if a row was released.
        """
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM stripe_events "
                "WHERE id=? AND processed_at IS NULL",
                (event_id,),
            )
            return (cur.rowcount or 0) > 0

    def get_stripe_event(self, event_id: str) -> Optional[dict]:
        """Fetch a stored Stripe event (for tests + debugging)."""
        row = self._conn().execute(
            "SELECT id, type, received_at, raw_json, processed_at "
            "FROM stripe_events WHERE id=?",
            (event_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "type": row[1],
            "received_at": row[2],
            "raw_json": row[3],
            "processed_at": row[4],
        }

    def find_workspace_by_stripe_customer(self,
                                           customer_id: str) -> Optional[dict]:
        """Look up a workspace by its Stripe customer id.

        Returns ``None`` (not a NotFound) when the customer is
        unknown — webhook handlers use this to map an inbound
        event back to the affected workspace.
        """
        row = self._conn().execute(
            "SELECT * FROM workspaces WHERE stripe_customer_id=?",
            (customer_id,),
        ).fetchone()
        if not row:
            return None
        # Mirror get_workspace's row-keyed dict shape.
        return {k: row[i] for i, k in enumerate(self._workspace_columns())}

    def _workspace_columns(self) -> list:
        """Column names in the workspaces table (cached)."""
        if not hasattr(self, "_ws_cols_cache"):
            cur = self._conn().execute(
                "SELECT * FROM workspaces LIMIT 0"
            )
            self._ws_cols_cache = [
                d[0] for d in cur.description
            ]
        return self._ws_cols_cache

    # ── Legacy compat: company_id <-> workspace_id aliases ─────

    def resolve_company_to_workspace(self, company_id: str) -> str:
        """Translate a legacy `cmp_*` id to its workspace_id.

        If the id already looks like a workspace_id (`wks_*`) we
        return it as-is. Otherwise we look it up in
        `legacy_company_alias` (the migration's backfill table) and
        return the corresponding workspace_id, or the input as a
        last resort (so calls against a fresh DB still work).
        """
        if company_id.startswith("wks_"):
            return company_id
        if not self._table_exists("legacy_company_alias"):
            return company_id
        row = self._conn().execute(
            "SELECT workspace_id FROM legacy_company_alias "
            "WHERE legacy_company_id=?",
            (company_id,),
        ).fetchone()
        if row:
            return row[0]
        # Fallback: maybe a workspace with that exact id was created
        # post-migration.  Try the workspaces table.
        try:
            self.get_workspace(company_id)
            return company_id
        except NotFound:
            pass
        # Last resort: return as-is so existing call sites raise 404
        # rather than 500.
        return company_id

    def create_company(self, *, slug: str, name: str, description: str = "") -> dict:
        """Legacy compat shim.

        Wave 1 callers (and the legacy `/api/v1/companies` test
        suite) invoke `create_company(slug=..., name=...)`.  We map
        this to a workspace create and return a dict whose
        `company_id` field carries the new workspace_id value, so
        test code reading `c["company_id"]` keeps working.
        """
        ws = self.create_workspace(slug=slug, name=name, description=description)
        return {
            "company_id": ws["workspace_id"],
            "workspace_id": ws["workspace_id"],
            "slug": ws["slug"],
            "name": ws["name"],
            "description": ws.get("description", ""),
            "plan": ws.get("plan", "free"),
            "status": ws.get("status", "active"),
            "live_enabled": ws.get("live_enabled", False),
            "owner_user_id": ws.get("owner_user_id"),
            "created_at": ws["created_at"],
            "updated_at": ws.get("updated_at"),
        }

    def get_company(self, company_id: str) -> dict:
        """Legacy compat shim: load by `company_id` (which we treat
        as a workspace_id, possibly translated via the alias table).
        """
        wid = self.resolve_company_to_workspace(company_id)
        ws = self.get_workspace(wid)
        return {
            "company_id": ws["workspace_id"],
            "workspace_id": ws["workspace_id"],
            "slug": ws["slug"],
            "name": ws["name"],
            "description": ws.get("description", ""),
            "created_at": ws["created_at"],
            "updated_at": ws["updated_at"],
        }

    def list_companies(self, workspace_id: Optional[str] = None) -> list[dict]:
        """Legacy compat shim: list workspaces as 'companies'.

        If `workspace_id` is set, returns only the row for that
        workspace.  Otherwise returns every workspace.
        """
        if workspace_id is not None:
            wid = self.resolve_company_to_workspace(workspace_id)
            try:
                ws = self.get_workspace(wid)
            except NotFound:
                return []
            return [{
                "company_id": ws["workspace_id"],
                "workspace_id": ws["workspace_id"],
                "slug": ws["slug"],
                "name": ws["name"],
                "description": ws.get("description", ""),
                "created_at": ws["created_at"],
                "updated_at": ws["updated_at"],
            }]
        rows = self.list_workspaces()
        return [
            {
                "company_id": r["workspace_id"],
                "workspace_id": r["workspace_id"],
                "slug": r["slug"],
                "name": r["name"],
                "description": r.get("description", ""),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def update_company(self, company_id: str, *,
                        name: Optional[str] = None,
                        description: Optional[str] = None) -> dict:
        """Legacy compat shim: update a workspace as if it were a
        company.  Returns the same shape as `get_company`.
        """
        wid = self.resolve_company_to_workspace(company_id)
        ws = self.update_workspace(wid, name=name, description=description)
        return {
            "company_id": ws["workspace_id"],
            "workspace_id": ws["workspace_id"],
            "slug": ws["slug"],
            "name": ws["name"],
            "description": ws.get("description", ""),
            "created_at": ws["created_at"],
            "updated_at": ws["updated_at"],
        }

    # ── Bible ─────────────────────────────────────────────────

    def create_bible(self, *, company_id: Optional[str] = None,
                        workspace_id: Optional[str] = None,
                        payload: dict, notes: str = "") -> dict:
        wid = self._resolve_wid(company_id, workspace_id)
        self.require_workspace(wid)
        with self.tx() as c:
            version = (c.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM bible_versions "
                "WHERE workspace_id=?",
                (wid,),
            ).fetchone()[0])
            bible_id = new_id("bbl")
            c.execute(
                "INSERT INTO bible_versions(bible_id, workspace_id, "
                "version, status, payload_json, notes, created_at, "
                "approved_at) VALUES (?,?,?,?,?,?,?,?)",
                (bible_id, wid, version, "draft",
                 json.dumps(payload), notes, now_iso(), None),
            )
        return self.get_bible(bible_id)

    def approve_bible(self, bible_id: str) -> dict:
        with self.tx() as c:
            row = c.execute(
                "SELECT workspace_id, status, version FROM bible_versions "
                "WHERE bible_id=?",
                (bible_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"bible {bible_id} not found")
            if row[1] != "draft":
                raise ValueError("only draft bibles can be approved")
            c.execute(
                "UPDATE bible_versions SET status='approved', approved_at=? "
                "WHERE bible_id=?",
                (now_iso(), bible_id),
            )
        return self.get_bible(bible_id)

    def get_bible(self, bible_id: str) -> dict:
        row = self._conn().execute(
            "SELECT bible_id, workspace_id, version, status, payload_json, "
            "notes, created_at, approved_at FROM bible_versions "
            "WHERE bible_id=?",
            (bible_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"bible {bible_id} not found")
        return self._row_bible(row)

    def list_bibles(self, workspace_id: str) -> list[dict]:
        self.require_workspace(workspace_id)
        rows = self._conn().execute(
            "SELECT bible_id, workspace_id, version, status, payload_json, "
            "notes, created_at, approved_at FROM bible_versions "
            "WHERE workspace_id=? ORDER BY version DESC",
            (workspace_id,),
        ).fetchall()
        return [self._row_bible(r) for r in rows]

    def list_bibles_by_company(self, company_id: str) -> list[dict]:
        """Alias for list_bibles(workspace_id=company_id).

        Translates a legacy company_id to workspace_id via the
        alias table and returns the bibles scoped to that
        workspace.
        """
        return self.list_bibles(company_id)

    def _row_bible(self, row) -> dict:
        return {
            "bible_id": row[0],
            "workspace_id": row[1],
            "company_id": row[1],  # legacy alias
            "version": row[2],
            "status": row[3],
            "payload": json.loads(row[4]) if row[4] else {},
            "notes": row[5] or "",
            "created_at": row[6],
            "approved_at": row[7],
        }

    # ── Assets ────────────────────────────────────────────────

    def create_asset(self, *, company_id: Optional[str] = None,
                        workspace_id: Optional[str] = None,
                        kind: str, filename: str,
                        storage_key: str, mime_type: str, size_bytes: int,
                        checksum: str) -> dict:
        wid = self._resolve_wid(company_id, workspace_id)
        self.require_workspace(wid)
        asset_id = new_id("ast")
        with self.tx() as c:
            c.execute(
                "INSERT INTO assets(asset_id, workspace_id, kind, filename, "
                "storage_key, mime_type, size_bytes, checksum, uploaded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (asset_id, wid, kind, filename, storage_key,
                 mime_type, size_bytes, checksum, now_iso()),
            )
        return self.get_asset(asset_id)

    def get_asset(self, asset_id: str) -> dict:
        row = self._conn().execute(
            "SELECT asset_id, workspace_id, kind, filename, storage_key, "
            "mime_type, size_bytes, checksum, uploaded_at FROM assets "
            "WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"asset {asset_id} not found")
        return self._row_asset(row)

    def list_assets(self, workspace_id: str) -> list[dict]:
        self.require_workspace(workspace_id)
        rows = self._conn().execute(
            "SELECT asset_id, workspace_id, kind, filename, storage_key, "
            "mime_type, size_bytes, checksum, uploaded_at FROM assets "
            "WHERE workspace_id=? ORDER BY uploaded_at DESC, rowid DESC",
            (workspace_id,),
        ).fetchall()
        return [self._row_asset(r) for r in rows]

    def list_assets_page(self, workspace_id: str, *,
                           limit: Optional[int] = None,
                           cursor: Optional[str] = None
                           ) -> tuple[list[dict], Optional[str]]:
        """Keyset page of assets, newest first (MAJ-4)."""
        from core.pagination import clamp_limit, keyset_page
        self.require_workspace(workspace_id)
        return keyset_page(
            self._conn(),
            select=("SELECT asset_id, workspace_id, kind, filename, "
                    "storage_key, mime_type, size_bytes, checksum, "
                    "uploaded_at FROM assets"),
            where="workspace_id=?", params=[workspace_id],
            ts_col="uploaded_at", limit=clamp_limit(limit), cursor=cursor,
            row_fn=self._row_asset,
        )

    def _row_asset(self, row) -> dict:
        return {
            "asset_id": row[0],
            "workspace_id": row[1],
            "company_id": row[1],  # legacy alias
            "kind": row[2],
            "filename": row[3],
            "storage_key": row[4],
            "mime_type": row[5] or "",
            "size_bytes": row[6] or 0,
            "checksum": row[7] or "",
            "uploaded_at": row[8],
        }

    # ── Jobs ──────────────────────────────────────────────────

    def create_job(self, *, company_id: Optional[str] = None,
                     workspace_id: Optional[str] = None,
                     topic: str, language: str,
                     duration_seconds: int, platforms: list[str],
                     aspect_ratio: str = "9:16", count: int = 1,
                     render_profile: str = "mock",
                     bible_id: Optional[str] = None,
                     approval_required: bool = False,
                     admission=None) -> dict:
        """Insert a job row in state ``queued``.

        ``admission`` (BLK-1): optional ``callable(conn, job_id)`` run
        inside the same ``BEGIN IMMEDIATE`` transaction as the INSERT,
        before it. It re-reads whatever it needs through ``conn``
        (job count this month, ...) and raises to refuse; the INSERT
        then never happens. Check + insert are one atomic unit across
        threads and processes, so two concurrent submits cannot both
        pass a "N jobs left" check.
        """
        wid = self._resolve_wid(company_id, workspace_id)
        self.require_workspace(wid)
        if bible_id:
            b = self.get_bible(bible_id)
            if b["workspace_id"] != wid:
                raise WorkspaceAccessDenied(
                    f"bible {bible_id} belongs to workspace "
                    f"{b['workspace_id']!r}; cannot use in workspace "
                    f"{wid!r}"
                )
            if b["status"] != "approved":
                raise ValueError("bible must be approved before job creation")
        job_id = new_id("job")
        t = now_iso()
        with (self.immediate_tx() if admission is not None else self.tx()) as c:
            if admission is not None:
                admission(c, job_id)
            c.execute(
                """INSERT INTO jobs(job_id, workspace_id, bible_id, topic, language,
                   duration_seconds, platforms_json, aspect_ratio, count,
                   render_profile, approval_required, created_at, updated_at,
                   state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, wid, bible_id, topic, language,
                 duration_seconds, json.dumps(platforms), aspect_ratio, count,
                 render_profile, 1 if approval_required else 0, t, t, "queued"),
            )
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields) -> dict:
        with self.tx() as c:
            row = c.execute(
                "SELECT workspace_id FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"job {job_id} not found")
            keys = list(fields.keys())
            sets = ",".join(f"{k}=?" for k in keys)
            vals = [fields[k] for k in keys]
            c.execute(
                f"UPDATE jobs SET {sets}, updated_at=? WHERE job_id=?",
                (*vals, now_iso(), job_id),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        row = self._conn().execute(
            """SELECT job_id, workspace_id, bible_id, topic, language,
               duration_seconds, platforms_json, aspect_ratio, count,
               render_profile, approval_required, created_at, updated_at,
               state, status_message, current_stage FROM jobs WHERE job_id=?""",
            (job_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"job {job_id} not found")
        return self._row_job(row)

    def count_jobs_since(self, *, workspace_id: str,
                            since_iso: str, conn=None) -> int:
        """Count how many jobs the workspace has submitted at or
        after ``since_iso`` (inclusive).

        ``conn``: pass the connection of an open ``immediate_tx()`` to
        count inside that transaction (atomic quota admission, BLK-1).

        Used by the Wave 6b FREE-tier monthly job quota
        (``core/quotas.check_job_quota``).  Pure SELECT; no tx
        required.  ``since_iso`` is the first instant of the
        current month in the same ISO format produced by
        ``now_iso`` (``YYYY-MM-%-dTHH:MM:SSZ``); SQLite compares
        ISO-8601 strings lexicographically and the format sorts
        chronologically, so ``>= since_iso`` is correct without a
        datetime cast.

        Cancelled / failed jobs are NOT excluded — the brief counts
        every job the user submitted, not just successful ones.
        That matches the operator's mental model: "I burnt my 5
        attempts on broken bibles" is a real quota cost.
        """
        row = (conn or self._conn()).execute(
            "SELECT COUNT(*) FROM jobs WHERE workspace_id=? "
            "AND created_at >= ?",
            (workspace_id, since_iso),
        ).fetchone()
        return int(row[0]) if row else 0

    def count_workspaces_owned_by(self, user_id: str) -> int:
        """Count how many workspaces the user owns (role='owner').

        Used by the Wave 6b FREE-tier workspace quota
        (``core/quotas.check_workspace_count``).  The Wave 2
        membership model grants a user the ``owner`` role only on
        workspaces they created (or were assigned as the first
        member of); the brief's "1 workspace per FREE user" limit
        counts only owner-role workspaces.
        """
        row = self._conn().execute(
            "SELECT COUNT(*) FROM memberships "
            "WHERE user_id=? AND role='owner'",
            (user_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def list_jobs(self, *, company_id: Optional[str] = None,
                    workspace_id: Optional[str] = None,
                    state: Optional[str] = None) -> list[dict]:
        wid = workspace_id
        if wid is None and company_id is not None:
            wid = company_id
        q = """SELECT job_id, workspace_id, bible_id, topic, language,
               duration_seconds, platforms_json, aspect_ratio, count,
               render_profile, approval_required, created_at, updated_at,
               state, status_message, current_stage FROM jobs WHERE 1=1"""
        params: list = []
        if wid:
            q += " AND workspace_id=?"
            params.append(wid)
        if state:
            q += " AND state=?"
            params.append(state)
        q += " ORDER BY created_at DESC, rowid DESC"
        rows = self._conn().execute(q, params).fetchall()
        return [self._row_job(r) for r in rows]

    _JOB_COLS = (
        "job_id, workspace_id, bible_id, topic, language, "
        "duration_seconds, platforms_json, aspect_ratio, count, "
        "render_profile, approval_required, created_at, updated_at, "
        "state, status_message, current_stage"
    )

    def list_jobs_page(self, *, company_id: Optional[str] = None,
                         workspace_id: Optional[str] = None,
                         state: Optional[str] = None,
                         limit: Optional[int] = None,
                         cursor: Optional[str] = None
                         ) -> tuple[list[dict], Optional[str]]:
        """Keyset page of jobs, newest first (MAJ-4).

        Returns ``(items, next_cursor)``; ``next_cursor`` is None on
        the last page. See core/pagination.py.
        """
        from core.pagination import clamp_limit, keyset_page
        wid = workspace_id if workspace_id is not None else company_id
        where, params = "1=1", []
        if wid:
            where += " AND workspace_id=?"
            params.append(wid)
        if state:
            where += " AND state=?"
            params.append(state)
        return keyset_page(
            self._conn(), select=f"SELECT {self._JOB_COLS} FROM jobs",
            where=where, params=params, ts_col="created_at",
            limit=clamp_limit(limit), cursor=cursor, row_fn=self._row_job,
        )

    # ── Restart recovery (F-30) ───────────────────────────────

    def recover_stale_running_jobs(self, *, older_than_seconds: float,
                                      max_requeues: int = 1,
                                      exclude_job_ids: Iterable[str] = (),
                                      now: Optional[float] = None) -> dict:
        """Requeue (once) or fail jobs stuck in ``running``.

        A job whose row has not been touched (``updated_at``) for
        ``older_than_seconds`` while in state ``running`` has no live
        pipeline thread (the process died mid-run). The first time we
        see it we put it back to ``queued`` and bump
        ``recovery_count``; once ``recovery_count`` reached
        ``max_requeues`` it is marked ``failed`` with a reason instead,
        so a job that crashes the worker cannot loop forever. Orphan
        ``running`` stage rows of those jobs are closed as ``failed``.
        One ``BEGIN IMMEDIATE`` transaction: two booting processes
        cannot both requeue the same job.

        Returns ``{"requeued": [job_id...], "failed": [job_id...]}``.
        """
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime((now if now is not None else time.time())
                        - float(older_than_seconds)),
        )
        skip = set(exclude_job_ids or ())
        requeued: list[str] = []
        failed: list[str] = []
        t = now_iso()
        with self.immediate_tx() as c:
            rows = c.execute(
                "SELECT job_id, recovery_count FROM jobs "
                "WHERE state='running' AND updated_at <= ? "
                "ORDER BY created_at, rowid",
                (cutoff,),
            ).fetchall()
            for job_id, rc in rows:
                if job_id in skip:
                    continue
                rc = int(rc or 0)
                if rc < max_requeues:
                    c.execute(
                        "UPDATE jobs SET state='queued', "
                        "recovery_count=?, status_message=?, "
                        "updated_at=? WHERE job_id=?",
                        (rc + 1,
                         f"recovered after restart: re-queued "
                         f"(attempt {rc + 1} of {max_requeues})",
                         t, job_id),
                    )
                    requeued.append(job_id)
                else:
                    c.execute(
                        "UPDATE jobs SET state='failed', "
                        "status_message=?, updated_at=? WHERE job_id=?",
                        (f"failed after restart: job was still running "
                         f"after {rc} recovery attempt(s); not retried "
                         f"again (stale for >= "
                         f"{int(older_than_seconds)}s)",
                         t, job_id),
                    )
                    failed.append(job_id)
                c.execute(
                    "UPDATE job_stages SET status='failed', "
                    "finished_at=COALESCE(finished_at, ?), "
                    "error=COALESCE(error, ?) "
                    "WHERE job_id=? AND status='running'",
                    (t, "interrupted: process restarted mid-stage",
                     job_id),
                )
        return {"requeued": requeued, "failed": failed}

    def list_queued_job_ids(self) -> list[str]:
        """Queued jobs, oldest first (FIFO order for resubmission)."""
        rows = self._conn().execute(
            "SELECT job_id FROM jobs WHERE state='queued' "
            "ORDER BY created_at, rowid"
        ).fetchall()
        return [r[0] for r in rows]

    def get_job_recovery_count(self, job_id: str) -> int:
        row = self._conn().execute(
            "SELECT recovery_count FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"job {job_id} not found")
        return int(row[0] or 0)

    def _row_job(self, row) -> dict:
        return {
            "job_id": row[0],
            "workspace_id": row[1],
            "company_id": row[1],  # legacy alias
            "bible_id": row[2],
            "topic": row[3],
            "language": row[4],
            "duration_seconds": row[5],
            "platforms": json.loads(row[6]) if row[6] else [],
            "aspect_ratio": row[7],
            "count": row[8],
            "render_profile": row[9],
            "approval_required": bool(row[10]),
            "created_at": row[11],
            "updated_at": row[12],
            "state": row[13],
            "status_message": row[14] or "",
            "current_stage": row[15] or "",
        }

    # ── Stages ────────────────────────────────────────────────

    def upsert_stage(self, *, job_id: str, stage_key: str, ordinal: int,
                        status: str, started_at: Optional[str] = None,
                        finished_at: Optional[str] = None,
                        duration_ms: Optional[int] = None,
                        summary: Optional[dict] = None,
                        error: Optional[str] = None) -> dict:
        stage_id = new_id("stg")
        with self.tx() as c:
            existing = c.execute(
                "SELECT stage_id FROM job_stages WHERE job_id=? AND stage_key=?",
                (job_id, stage_key),
            ).fetchone()
            if existing:
                stage_id = existing[0]
                c.execute(
                    "UPDATE job_stages SET status=?, "
                    "started_at=COALESCE(?,started_at), "
                    "finished_at=COALESCE(?,finished_at), "
                    "duration_ms=COALESCE(?,duration_ms), "
                    "summary_json=COALESCE(?,summary_json), error=? "
                    "WHERE stage_id=?",
                    (status, started_at, finished_at, duration_ms,
                     json.dumps(summary) if summary is not None else None,
                     error, stage_id),
                )
            else:
                c.execute(
                    "INSERT INTO job_stages(stage_id, job_id, stage_key, "
                    "ordinal, status, started_at, finished_at, "
                    "duration_ms, summary_json, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (stage_id, job_id, stage_key, ordinal, status,
                     started_at, finished_at, duration_ms,
                     json.dumps(summary) if summary is not None else None,
                     error),
                )
        return self.get_stage(stage_id)

    def get_stage(self, stage_id: str) -> dict:
        row = self._conn().execute(
            "SELECT stage_id, job_id, stage_key, ordinal, status, "
            "started_at, finished_at, duration_ms, summary_json, error "
            "FROM job_stages WHERE stage_id=?",
            (stage_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"stage {stage_id} not found")
        return self._row_stage(row)

    def list_stages(self, job_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT stage_id, job_id, stage_key, ordinal, status, "
            "started_at, finished_at, duration_ms, summary_json, error "
            "FROM job_stages WHERE job_id=? ORDER BY ordinal",
            (job_id,),
        ).fetchall()
        return [self._row_stage(r) for r in rows]

    def _row_stage(self, row) -> dict:
        return {
            "stage_id": row[0],
            "job_id": row[1],
            "stage_key": row[2],
            "ordinal": row[3],
            "status": row[4],
            "started_at": row[5],
            "finished_at": row[6],
            "duration_ms": row[7],
            "summary": json.loads(row[8]) if row[8] else {},
            "error": row[9],
        }

    # ── Outputs ───────────────────────────────────────────────

    def create_output(self, job_id: str, *, storage_key: str, filename: str,
                        mime_type: str, size_bytes: int, checksum: str,
                        is_mock: bool = True, kind: str = "video") -> dict:
        output_id = new_id("out")
        with self.tx() as c:
            c.execute(
                "INSERT INTO job_outputs(output_id, job_id, storage_key, "
                "filename, mime_type, size_bytes, checksum, is_mock, kind, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (output_id, job_id, storage_key, filename, mime_type,
                 size_bytes, checksum, 1 if is_mock else 0, kind, now_iso()),
            )
        return self.get_output(output_id)

    def get_output(self, output_id: str) -> dict:
        row = self._conn().execute(
            "SELECT output_id, job_id, storage_key, filename, mime_type, "
            "size_bytes, checksum, is_mock, kind, created_at "
            "FROM job_outputs WHERE output_id=?",
            (output_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"output {output_id} not found")
        return self._row_output(row)

    def list_outputs(self, job_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT output_id, job_id, storage_key, filename, mime_type, "
            "size_bytes, checksum, is_mock, kind, created_at "
            "FROM job_outputs WHERE job_id=? ORDER BY created_at",
            (job_id,),
        ).fetchall()
        return [self._row_output(r) for r in rows]

    def _row_output(self, row) -> dict:
        return {
            "output_id": row[0],
            "job_id": row[1],
            "storage_key": row[2],
            "filename": row[3],
            "mime_type": row[4] or "",
            "size_bytes": row[5] or 0,
            "checksum": row[6] or "",
            "is_mock": bool(row[7]),
            "kind": row[8],
            "created_at": row[9],
        }

    # ── Publishing ────────────────────────────────────────────

    def create_publish_intent(self, *, job_id: str,
                                company_id: Optional[str] = None,
                                workspace_id: Optional[str] = None,
                                action: str, platforms: list[str],
                                caption: str = "",
                                scheduled_for: Optional[str] = None) -> dict:
        # F7: cross-workspace guard.  A publish intent must
        # reference a job that actually belongs to the calling
        # workspace.  Without this check, any caller could file a
        # publish intent for another tenant's job — they would see
        # the intent detail and could potentially pivot to live
        # publishing once enabled.
        wid = self._resolve_wid(company_id, workspace_id)
        self.require_workspace(wid)
        job = self.get_job(job_id)
        if job["workspace_id"] != wid:
            raise WorkspaceAccessDenied(
                f"job {job_id} belongs to workspace {job['workspace_id']!r}; "
                f"cannot publish on behalf of {wid!r}"
            )
        if action not in ("draft", "schedule", "publish"):
            raise ValueError(f"invalid action {action}")
        # Validate that platforms are recognized by the intake validator.
        try:
            from control_plane.campaign import PlatformSpec
            for p in platforms:
                PlatformSpec.from_name(p)
        except ValueError as e:
            raise ValueError(str(e)) from e
        intent_id = new_id("pub")
        with self.tx() as c:
            c.execute(
                "INSERT INTO publish_intents(intent_id, job_id, workspace_id, "
                "action, platforms_json, caption, scheduled_for, created_at, "
                "status) VALUES (?,?,?,?,?,?,?,?,?)",
                (intent_id, job_id, wid, action,
                 json.dumps(platforms), caption, scheduled_for, now_iso(),
                 "pending"),
            )
        return self.get_publish_intent(intent_id)

    def update_publish_intent(self, intent_id: str, *, status: str,
                                detail: str = "") -> dict:
        with self.tx() as c:
            c.execute(
                "UPDATE publish_intents SET status=?, executed_at=?, detail=? "
                "WHERE intent_id=?",
                (status, now_iso(), detail, intent_id),
            )
        return self.get_publish_intent(intent_id)

    def get_publish_intent(self, intent_id: str) -> dict:
        row = self._conn().execute(
            "SELECT intent_id, job_id, workspace_id, action, platforms_json, "
            "caption, scheduled_for, created_at, executed_at, status, detail "
            "FROM publish_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"intent {intent_id} not found")
        return self._row_intent(row)

    def list_publish_intents(self, job_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT intent_id, job_id, workspace_id, action, platforms_json, "
            "caption, scheduled_for, created_at, executed_at, status, detail "
            "FROM publish_intents WHERE job_id=? "
            "ORDER BY created_at DESC, rowid DESC",
            (job_id,),
        ).fetchall()
        return [self._row_intent(r) for r in rows]

    def list_publish_intents_page(self, job_id: str, *,
                                    limit: Optional[int] = None,
                                    cursor: Optional[str] = None
                                    ) -> tuple[list[dict], Optional[str]]:
        """Keyset page of publish intents, newest first (MAJ-4)."""
        from core.pagination import clamp_limit, keyset_page
        return keyset_page(
            self._conn(),
            select=("SELECT intent_id, job_id, workspace_id, action, "
                    "platforms_json, caption, scheduled_for, created_at, "
                    "executed_at, status, detail FROM publish_intents"),
            where="job_id=?", params=[job_id], ts_col="created_at",
            limit=clamp_limit(limit), cursor=cursor,
            row_fn=self._row_intent,
        )

    def _row_intent(self, row) -> dict:
        return {
            "intent_id": row[0],
            "job_id": row[1],
            "workspace_id": row[2],
            "company_id": row[2],  # legacy alias
            "action": row[3],
            "platforms": json.loads(row[4]) if row[4] else [],
            "caption": row[5] or "",
            "scheduled_for": row[6],
            "created_at": row[7],
            "executed_at": row[8],
            "status": row[9],
            "detail": row[10] or "",
        }

    # ── Integrations ──────────────────────────────────────────

    def list_integrations(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT name, enabled, mode, detail, updated_at "
            "FROM integrations ORDER BY name"
        ).fetchall()
        return [
            {"name": r[0], "enabled": bool(r[1]), "mode": r[2],
             "detail": r[3] or "", "updated_at": r[4]}
            for r in rows
        ]

    # ── Credentials (F-credential-vault) ─────────────────────

    # Providers we will accept secrets for. Anything outside this set
    # is rejected at the repo layer so a typo cannot silently store
    # a blob.
    CREDENTIAL_PROVIDERS = {
        "heygen_direct", "comfy_cloud", "comfy_gpu_worker",
        "comfy_heygen_partner", "publer",
    }

    def upsert_credential(self, *, company_id: Optional[str] = None,
                            workspace_id: Optional[str] = None,
                            provider: str,
                            fingerprint_str: str, encrypted_blob: bytes,
                            updated_by: str) -> dict:
        """Store or replace the encrypted credential for
        (workspace, provider).  The plaintext never enters the repo:
        callers encrypt via control_plane.secret_vault and pass
        the bytes.  Returns the masked surface (NEVER the
        plaintext).

        Wave 5: refuses to write while a master-key rotation is in
        progress.  The API maps this to HTTP 503 with a clear
        "rotation in progress" message so the UI can render its
        banner.

        `company_id` is accepted as a backwards-compat alias for
        `workspace_id` so legacy call sites keep working.  Exactly
        one of the two MUST be set; if both are set, `workspace_id`
        wins.
        """
        # Wave 5: refuse new credentials while a rotation runs.
        from control_plane import key_rotation
        if not key_rotation.credential_writes_allowed(self):
            raise key_rotation.RotationInProgress(
                "master-key rotation in progress; "
                "credential writes are paused"
            )
        if provider not in self.CREDENTIAL_PROVIDERS:
            raise ValueError(f"unknown provider: {provider!r}")
        wid = self._resolve_wid(company_id, workspace_id)
        self.require_workspace(wid)
        with self.tx() as c:
            existing = c.execute(
                "SELECT credential_id FROM credentials "
                "WHERE workspace_id=? AND provider=?",
                (wid, provider),
            ).fetchone()
            if existing:
                c.execute(
                    """UPDATE credentials
                       SET encrypted_blob=?, fingerprint=?, updated_at=?,
                           credential_id=?
                       WHERE workspace_id=? AND provider=?""",
                    (encrypted_blob, fingerprint_str, now_iso(),
                     existing[0], wid, provider),
                )
                cid = existing[0]
            else:
                cid = new_id("cred")
                c.execute(
                    """INSERT INTO credentials(credential_id, workspace_id,
                                              provider, encrypted_blob,
                                              fingerprint, updated_at,
                                              updated_by)
                       VALUES (?,?,?,?,?,?,?)""",
                    (cid, wid, provider, encrypted_blob,
                     fingerprint_str, now_iso(), updated_by),
                )
        return self.get_credential(wid, provider)

    def get_credential(self, workspace_id: str, provider: str) -> dict:
        row = self._conn().execute(
            "SELECT credential_id, workspace_id, provider, fingerprint, "
            "updated_at, updated_by FROM credentials "
            "WHERE workspace_id=? AND provider=?",
            (workspace_id, provider),
        ).fetchone()
        if not row:
            raise NotFound(
                f"credential {provider!r} for workspace {workspace_id!r} "
                f"not found"
            )
        return {
            "credential_id": row[0],
            "workspace_id": row[1],
            "company_id": row[1],  # legacy alias
            "provider": row[2],
            "has_value": True,
            "fingerprint": row[3],
            "updated_at": row[4],
            "updated_by": row[5],
        }

    def list_credentials(self, workspace_id: str) -> list[dict]:
        self.require_workspace(workspace_id)
        rows = self._conn().execute(
            "SELECT credential_id, provider, fingerprint, updated_at, "
            "updated_by FROM credentials WHERE workspace_id=? "
            "ORDER BY provider",
            (workspace_id,),
        ).fetchall()
        return [
            {
                "credential_id": r[0],
                "workspace_id": workspace_id,
                "company_id": workspace_id,  # legacy alias
                "provider": r[1],
                "has_value": True,
                "fingerprint": r[2],
                "updated_at": r[3],
                "updated_by": r[4],
            }
            for r in rows
        ]

    def delete_credential(self, *, company_id: Optional[str] = None,
                            workspace_id: Optional[str] = None,
                            provider: str) -> None:
        wid = self._resolve_wid(company_id, workspace_id)
        with self.tx() as c:
            existing = c.execute(
                "SELECT credential_id FROM credentials "
                "WHERE workspace_id=? AND provider=?",
                (wid, provider),
            ).fetchone()
            if not existing:
                raise NotFound(
                    f"credential {provider!r} for workspace {wid!r} "
                    f"not found"
                )
            c.execute(
                "DELETE FROM credentials WHERE workspace_id=? AND provider=?",
                (wid, provider),
            )

    def decrypt_credential(self, *, company_id: Optional[str] = None,
                              workspace_id: Optional[str] = None,
                              provider: str) -> str:
        """Return the plaintext for (workspace, provider).

        INTERNAL USE ONLY — never exposed through any API surface.
        The render plane calls this at submit() time. Any caller
        that surfaces the return through JSON is the leak.
        """
        wid = self._resolve_wid(company_id, workspace_id)
        from control_plane.secret_vault import (
            decrypt as _decrypt,
            SecretReadError,
        )
        row = self._conn().execute(
            "SELECT encrypted_blob FROM credentials "
            "WHERE workspace_id=? AND provider=?",
            (wid, provider),
        ).fetchone()
        if not row:
            raise NotFound(
                f"no credential for {wid}@{provider}"
            )
        try:
            return _decrypt(wid, provider, row[0])
        except SecretReadError as e:
            raise SecretReadError(
                f"credential for {wid}@{provider} unreadable: {e}. "
                f"Likely cause: VIDEO_FACTORY_SECRET_KEY changed since the "
                f"secret was written. Operator must re-enter the secret."
            ) from e

    # ── Auth: users ───────────────────────────────────────────
    # Wave 1: users/workspaces/sessions/memberships live here too
    # so the DB lock + per-thread connection handling stay
    # centralized.

    def create_user(self, *, email: str, password_hash: str) -> dict:
        user_id = new_id("usr")
        t = now_iso()
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO users(user_id, email, password_hash, "
                    "status, email_verified, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, email.lower(), password_hash, "active", 0, t),
                )
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                raise ValueError(
                    f"email {email!r} already exists"
                ) from e
            raise
        return self.get_user(user_id)

    def get_user_by_email(self, email: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT user_id, email, password_hash, status, email_verified, "
            "created_at, last_login_at FROM users WHERE email=?",
            (email.lower(),),
        ).fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "email": row[1],
            "password_hash": row[2],
            "status": row[3],
            "email_verified": bool(row[4]),
            "created_at": row[5],
            "last_login_at": row[6],
        }

    def get_user(self, user_id: str) -> dict:
        row = self._conn().execute(
            "SELECT user_id, email, password_hash, status, email_verified, "
            "created_at, last_login_at FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"user {user_id} not found")
        return {
            "user_id": row[0],
            "email": row[1],
            "password_hash": row[2],
            "status": row[3],
            "email_verified": bool(row[4]),
            "created_at": row[5],
            "last_login_at": row[6],
        }

    def update_user_last_login(self, user_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET last_login_at=? WHERE user_id=?",
                (now_iso(), user_id),
            )

    def update_user_status(self, user_id: str, *, status: str) -> None:
        if status not in ("active", "locked", "pending"):
            raise ValueError(f"invalid user status: {status!r}")
        with self.tx() as c:
            c.execute(
                "UPDATE users SET status=? WHERE user_id=?",
                (status, user_id),
            )

    def mark_user_email_verified(self, user_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET email_verified=1 WHERE user_id=?",
                (user_id,),
            )

    def list_workspaces_for_user(self, user_id: str) -> list[dict]:
        """Return all workspaces the user is a member of, with role."""
        rows = self._conn().execute(
            "SELECT m.workspace_id, m.role, w.slug, w.name, w.plan, w.status "
            "FROM memberships m JOIN workspaces w ON w.workspace_id = "
            "m.workspace_id WHERE m.user_id=? ORDER BY w.created_at, w.rowid",
            (user_id,),
        ).fetchall()
        return [
            {
                "workspace_id": r[0],
                "role": r[1],
                "slug": r[2],
                "name": r[3],
                "plan": r[4],
                "status": r[5],
            }
            for r in rows
        ]

    def get_membership(self, user_id: str, workspace_id: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT user_id, workspace_id, role FROM memberships "
            "WHERE user_id=? AND workspace_id=?",
            (user_id, workspace_id),
        ).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "workspace_id": row[1], "role": row[2]}

    def add_membership(self, *, user_id: str, workspace_id: str,
                        role: str) -> dict:
        if role not in ("owner", "admin", "editor", "viewer"):
            raise ValueError(f"invalid role: {role!r}")
        # Verify both ends exist; cheaper than the FK error.
        self.get_user(user_id)
        self.get_workspace(workspace_id)
        t = now_iso()
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO memberships(user_id, workspace_id, "
                "role, created_at) VALUES (?,?,?,?)",
                (user_id, workspace_id, role, t),
            )
        return {"user_id": user_id, "workspace_id": workspace_id,
                "role": role, "created_at": t}

    def list_memberships(self, user_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT user_id, workspace_id, role, created_at FROM memberships "
            "WHERE user_id=?",
            (user_id,),
        ).fetchall()
        return [
            {"user_id": r[0], "workspace_id": r[1], "role": r[2],
             "created_at": r[3]}
            for r in rows
        ]

    def remove_membership(self, *, user_id: str, workspace_id: str) -> bool:
        """Remove a membership. Refuses to remove the owner.

        Returns True if a row was deleted, False if the membership
        did not exist. Raises ValueError if the user is the
        workspace owner.
        """
        ws = self.get_workspace(workspace_id)
        if ws.get("owner_user_id") == user_id:
            raise ValueError(
                "cannot remove the workspace owner; transfer ownership first"
            )
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM memberships WHERE user_id=? AND workspace_id=?",
                (user_id, workspace_id),
            )
            return cur.rowcount > 0

    # ── Auth: sessions ────────────────────────────────────────

    def create_session(self, *, user_id: str, csrf_token: str,
                        ip: Optional[str], user_agent: Optional[str],
                        expires_at: str,
                        session_id: Optional[str] = None) -> dict:
        """Create a session row.

        `session_id` is generated by the caller (using a
        URL-safe token) and is what goes into the cookie. The
        default `new_id("ses")` would be too short to be
        unpredictable; callers MUST pass their own id. We allow
        the default for tests only.
        """
        sid = session_id or new_id("ses")
        t = now_iso()
        with self.tx() as c:
            c.execute(
                "INSERT INTO sessions(session_id, user_id, csrf_token, ip, "
                "user_agent, expires_at, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, user_id, csrf_token, ip, user_agent,
                 expires_at, t),
            )
        return {
            "session_id": sid,
            "user_id": user_id,
            "csrf_token": csrf_token,
            "ip": ip,
            "user_agent": user_agent,
            "expires_at": expires_at,
            "created_at": t,
        }

    def get_session(self, session_id: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT session_id, user_id, csrf_token, ip, user_agent, "
            "expires_at, created_at, last_seen_at FROM sessions "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "session_id": row[0],
            "user_id": row[1],
            "csrf_token": row[2],
            "ip": row[3],
            "user_agent": row[4],
            "expires_at": row[5],
            "created_at": row[6],
            "last_seen_at": row[7],
        }

    def delete_session(self, session_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "DELETE FROM sessions WHERE session_id=?",
                (session_id,),
            )

    def delete_sessions_for_user(self, user_id: str) -> int:
        """Force-logout: remove every session for a user. Returns count."""
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM sessions WHERE user_id=?", (user_id,),
            )
            return cur.rowcount

    def gc_expired_sessions(self) -> int:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM sessions WHERE expires_at < ?",
                (now_iso(),),
            )
            return cur.rowcount

    def update_session_last_seen(self, session_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE sessions SET last_seen_at=? WHERE session_id=?",
                (now_iso(), session_id),
            )

    # ── Auth: email verifications / password resets ───────────

    def create_email_verification(self, *, user_id: str, token: str,
                                    expires_at: str) -> dict:
        with self.tx() as c:
            c.execute(
                "INSERT INTO email_verifications(token, user_id, "
                "expires_at, created_at) VALUES (?,?,?,?)",
                (token, user_id, expires_at, now_iso()),
            )
        return {"token": token, "user_id": user_id, "expires_at": expires_at,
                "consumed_at": None, "created_at": now_iso()}

    def get_email_verification(self, token: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT token, user_id, expires_at, consumed_at, created_at "
            "FROM email_verifications WHERE token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        return {"token": row[0], "user_id": row[1], "expires_at": row[2],
                "consumed_at": row[3], "created_at": row[4]}

    def consume_email_verification(self, token: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE email_verifications SET consumed_at=? WHERE token=?",
                (now_iso(), token),
            )

    def create_password_reset(self, *, user_id: str, token: str,
                                expires_at: str) -> dict:
        with self.tx() as c:
            c.execute(
                "INSERT INTO password_resets(token, user_id, expires_at, "
                "created_at) VALUES (?,?,?,?)",
                (token, user_id, expires_at, now_iso()),
            )
        return {"token": token, "user_id": user_id, "expires_at": expires_at,
                "consumed_at": None, "created_at": now_iso()}

    def get_password_reset(self, token: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT token, user_id, expires_at, consumed_at, created_at "
            "FROM password_resets WHERE token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        return {"token": row[0], "user_id": row[1], "expires_at": row[2],
                "consumed_at": row[3], "created_at": row[4]}

    def consume_password_reset(self, token: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE password_resets SET consumed_at=? WHERE token=?",
                (now_iso(), token),
            )

    # ── Audit log (auth + workspace) ──────────────────────────

    def append_audit(self, *, event: str, context: Optional[dict] = None,
                        user_id: Optional[str] = None,
                        workspace_id: Optional[str] = None,
                        actor: Optional[str] = None) -> dict:
        """Append a row to audit_log.

        MAJ-1: this is the ONLY code path that writes ``audit_log``.
        ``auth.audit_log.append``, ``core.audit.emit`` and the
        ``audit_log`` mirror in ``control_plane.repo_helpers.
        append_runner_audit`` all delegate here.

        `actor` is the denormalized display name (email or
        'operator') so audit consumers do not need to join users
        at read time. Best-effort: never raises; a dropped row is
        logged as a warning (logger ``auth.audit_log``) so the
        operator sees it.
        """
        try:
            audit_id = f"audlog_{uuid.uuid4().hex[:12]}"
            self._conn().execute(
                "INSERT INTO audit_log(audit_id, event, context_json, "
                "user_id, workspace_id, actor, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (audit_id, event, json.dumps(context or {}), user_id,
                 workspace_id, actor, now_iso()),
            )
            self._conn().commit()
            return {"audit_id": audit_id, "event": event,
                    "context": context or {},
                    "user_id": user_id, "workspace_id": workspace_id,
                    "actor": actor}
        except Exception as e:
            # Best-effort: never let an audit failure break the
            # caller (a missed audit row is better than a missed
            # login / billing action), but surface it.
            try:
                from auth.audit_log import _logger as _audit_logger
                _audit_logger.warning(
                    "audit_append dropped row event=%s user=%s "
                    "workspace=%s err=%s: %s",
                    event, user_id, workspace_id, type(e).__name__, e,
                )
            except Exception:
                pass
            return {"audit_id": "", "event": event, "context": {},
                    "user_id": user_id, "workspace_id": workspace_id,
                    "actor": actor}

    @staticmethod
    def _row_audit(r) -> dict:
        return {
            "audit_id": r[0],
            "event": r[1],
            "context": json.loads(r[2]) if r[2] else {},
            "user_id": r[3],
            "workspace_id": r[4],
            "actor": r[5],
            "created_at": r[6],
        }

    def list_audit(self, *, workspace_id: str, limit: int = 100) -> list[dict]:
        """Return recent audit rows for a workspace, newest first."""
        rows = self._conn().execute(
            "SELECT audit_id, event, context_json, user_id, workspace_id, "
            "actor, created_at FROM audit_log WHERE workspace_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (workspace_id, int(limit)),
        ).fetchall()
        return [self._row_audit(r) for r in rows]

    def list_audit_page(self, *, workspace_id: str,
                          limit: Optional[int] = None,
                          cursor: Optional[str] = None
                          ) -> tuple[list[dict], Optional[str]]:
        """Keyset page of audit rows, newest first (MAJ-4).

        Default page size stays 100 (the pre-pagination default of
        GET /workspaces/{wid}/audit); max 200.
        """
        from core.pagination import clamp_limit, keyset_page
        return keyset_page(
            self._conn(),
            select=("SELECT audit_id, event, context_json, user_id, "
                    "workspace_id, actor, created_at FROM audit_log"),
            where="workspace_id=?", params=[workspace_id],
            ts_col="created_at", limit=clamp_limit(limit, default=100),
            cursor=cursor, row_fn=self._row_audit,
        )

    # ── Retention (F-42) ─────────────────────────────────────

    def delete_audit_older_than(self, cutoff_iso: str) -> dict:
        """Delete ``audit_log`` and ``runner_audit`` rows created
        strictly before ``cutoff_iso``. Returns per-table counts."""
        out = {}
        with self.immediate_tx() as c:
            for table in ("audit_log", "runner_audit"):
                exists = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name=?", (table,),
                ).fetchone()
                if not exists:
                    out[table] = 0
                    continue
                cur = c.execute(
                    f"DELETE FROM {table} WHERE created_at < ?",
                    (cutoff_iso,),
                )
                out[table] = int(cur.rowcount or 0)
        return out

    # ── Auth: alias for the Wave 1 -> Wave 2 transition ───────

    def workspace_id_for_company(self, company_id: str) -> Optional[str]:
        """Return the workspace_id for a company_id, if one exists.

        The legacy `company_id` form is preserved in test code and
        in the `/api/v1/companies/{cid}` alias.  Returns None for
        unknown ids.
        """
        return self.resolve_company_to_workspace(company_id)

    # ── Wave 4 OIDC helpers ─────────────────────────────────

    def create_oauth_identity(
        self, *, user_id: str, provider: str, provider_sub: str,
    ) -> dict:
        """Link an OIDC identity (provider + sub) to an existing user.

        Idempotent: if the (provider, provider_sub) pair is already
        linked to `user_id`, returns the existing row. If it is
        linked to a DIFFERENT user, raises ValueError so the caller
        can refuse to steal another account's identity.
        """
        provider = provider.lower()
        with self.tx() as c:
            existing = c.execute(
                "SELECT oauth_id, user_id, provider, provider_sub, "
                "created_at FROM oauth_identities WHERE provider=? AND "
                "provider_sub=?",
                (provider, provider_sub),
            ).fetchone()
            if existing:
                if existing[1] != user_id:
                    raise ValueError(
                        f"OIDC identity {provider}:{provider_sub} is already "
                        f"linked to user {existing[1]} (not {user_id})"
                    )
                return {
                    "oauth_id": existing[0],
                    "user_id": existing[1],
                    "provider": existing[2],
                    "provider_sub": existing[3],
                    "created_at": existing[4],
                }
            oauth_id = new_id("oauth")
            c.execute(
                "INSERT INTO oauth_identities(oauth_id, user_id, provider, "
                "provider_sub, created_at) VALUES (?,?,?,?,?)",
                (oauth_id, user_id, provider, provider_sub, now_iso()),
            )
            return {
                "oauth_id": oauth_id,
                "user_id": user_id,
                "provider": provider,
                "provider_sub": provider_sub,
                "created_at": now_iso(),
            }

    def get_oauth_identity(
        self, *, provider: str, provider_sub: str,
    ) -> Optional[dict]:
        """Return the oauth_identity row for (provider, provider_sub), or None."""
        row = self._conn().execute(
            "SELECT oauth_id, user_id, provider, provider_sub, created_at "
            "FROM oauth_identities WHERE provider=? AND provider_sub=?",
            (provider.lower(), provider_sub),
        ).fetchone()
        if not row:
            return None
        return {
            "oauth_id": row[0],
            "user_id": row[1],
            "provider": row[2],
            "provider_sub": row[3],
            "created_at": row[4],
        }

    def list_oauth_identities_for_user(self, user_id: str) -> list[dict]:
        """Return every oauth identity linked to this user."""
        rows = self._conn().execute(
            "SELECT oauth_id, provider, provider_sub, created_at "
            "FROM oauth_identities WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [
            {
                "oauth_id": r[0],
                "provider": r[1],
                "provider_sub": r[2],
                "created_at": r[3],
            }
            for r in rows
        ]

    def delete_oauth_identity(self, *, user_id: str, provider: str,
                              provider_sub: str) -> bool:
        """Disconnect a (provider, provider_sub) from the user.

        Refuses to remove the LAST identity for a user that has no
        password set — without *something* to authenticate with, the
        account becomes permanently unreachable.  Raises ValueError
        if the disconnect would orphan the account.
        """
        provider = provider.lower()
        with self.tx() as c:
            row = c.execute(
                "SELECT oauth_id FROM oauth_identities WHERE user_id=? AND "
                "provider=? AND provider_sub=?",
                (user_id, provider, provider_sub),
            ).fetchone()
            if not row:
                return False
            user_row = c.execute(
                "SELECT password_hash FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
            password_disabled = (
                user_row is None
                or user_row[0] is None
                or user_row[0] == "disabled"
            )
            if password_disabled:
                remaining = c.execute(
                    "SELECT COUNT(*) FROM oauth_identities WHERE user_id=?",
                    (user_id,),
                ).fetchone()[0]
                if remaining <= 1:
                    raise ValueError(
                        "cannot remove last authentication method: "
                        "user has no password set"
                    )
            c.execute(
                "DELETE FROM oauth_identities WHERE oauth_id=?",
                (row[0],),
            )
            return True
