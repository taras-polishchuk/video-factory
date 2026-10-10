"""SQLite migrations for Video Factory.

Each migration is a small SQL fragment (or a registered Python
function) that the Repo applies at bootstrap. Applied ids are recorded
in the `schema_migrations` ledger, so every migration runs exactly
once per database (F-11). Migrations are append-only — never edit a
migration that has shipped. To change a table, add a new migration.

Schema invariants enforced here:

- `users.email` is globally unique
- `workspaces.slug` is globally unique
- `memberships` PK is (user_id, workspace_id) — one role per user per workspace
- `sessions.id` is a random opaque token; csrf_token is HMAC-derived
- `audit_log` carries optional user_id + workspace_id for actor attribution
- All FKs scoped to a workspace so cross-workspace access can be
  rejected at the repo layer.

Wave 1 (auth foundations) ships migrations 001..004. Future waves
extend with 005+ (workspace_id rename, SQLCipher, etc.).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Tuple


# Ordered list of (id, description, sql). Each entry is applied in
# order. All SQL is idempotent (CREATE TABLE IF NOT EXISTS, etc.).
# A migration that requires code coordination (e.g. dropping a column)
# must include the corresponding code change in the same commit.
MIGRATIONS: List[Tuple[str, str, str]] = [
    (
        "001",
        "users, workspaces, memberships (auth foundations)",
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'locked', 'pending')),
            email_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

        CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id TEXT PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free'
                CHECK (plan IN ('free', 'pro', 'enterprise')),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'deleted')),
            live_enabled INTEGER NOT NULL DEFAULT 0,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_user_id) REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON workspaces(owner_user_id);

        CREATE TABLE IF NOT EXISTS memberships (
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            role TEXT NOT NULL
                CHECK (role IN ('owner', 'admin', 'editor', 'viewer')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, workspace_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
        );
        CREATE INDEX IF NOT EXISTS idx_memberships_workspace ON memberships(workspace_id);
        """,
    ),
    (
        "002",
        "sessions, login_attempts, email_verifications, password_resets",
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            csrf_token TEXT NOT NULL,
            ip TEXT,
            user_agent TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

        CREATE TABLE IF NOT EXISTS login_attempts (
            attempt_id TEXT PRIMARY KEY,
            ip TEXT NOT NULL,
            email TEXT NOT NULL,
            succeeded INTEGER NOT NULL DEFAULT 0,
            attempted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, attempted_at);
        CREATE INDEX IF NOT EXISTS idx_login_attempts_email ON login_attempts(email, attempted_at);

        CREATE TABLE IF NOT EXISTS email_verifications (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_email_verifications_user
            ON email_verifications(user_id);

        CREATE TABLE IF NOT EXISTS password_resets (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_password_resets_user
            ON password_resets(user_id);
        """,
    ),
    (
        "003",
        "extend audit_log with user_id + workspace_id; add audit_log table if absent",
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            audit_id TEXT PRIMARY KEY,
            event TEXT NOT NULL,
            context_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event);
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
        """,
    ),
    (
        "004",
        "ALTER audit_log: add user_id + workspace_id columns if missing",
        # SQLite has no IF NOT EXISTS for ALTER TABLE ADD COLUMN prior
        # to 3.35; we run via PRAGMA table_info check in bootstrap
        # and apply manually here. This fragment is intentionally
        # empty — the column additions are handled by
        # `_extend_audit_log_columns()` which is also called from
        # bootstrap. See core/repo.py.
        "SELECT 1;",
    ),
    (
        "005",
        "rename company_id -> workspace_id across legacy tables; "
        "backfill from companies into workspaces; drop companies",
        # This migration is applied via _apply_migration_005() in
        # core/repo.py because SQLite has no RENAME COLUMN on older
        # versions and the backfill must touch multiple tables in a
        # deterministic order. The empty SQL here is intentional.
        "SELECT 1;",
    ),
    (
        "006",
        "audit_log.actor column (denormalized actor display name)",
        # Handled by _extend_audit_log_columns() in core/repo.py.
        "SELECT 1;",
    ),
    (
        "007",
        "oauth_identities table (Wave 4 OIDC: Google + GitHub sign-in)",
        """
        CREATE TABLE IF NOT EXISTS oauth_identities (
            oauth_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_sub TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (provider, provider_sub),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_oauth_identities_user
            ON oauth_identities(user_id);
        CREATE INDEX IF NOT EXISTS idx_oauth_identities_provider
            ON oauth_identities(provider, provider_sub);
        """,
    ),
    (
        "008",
        "workspaces.current_period_end (Wave 6c Stripe webhook billing cycle)",
        # Handled by _extend_workspaces_columns() in core/repo.py.
        # SQLite has no IF NOT EXISTS for ADD COLUMN prior to 3.35;
        # we add the column via PRAGMA table_info check at bootstrap.
        "SELECT 1;",
    ),
    (
        "009",
        "stripe_events (Wave 6c Stripe webhook idempotency + audit)",
        """
        CREATE TABLE IF NOT EXISTS stripe_events (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            received_at INTEGER NOT NULL,
            raw_json TEXT NOT NULL,
            processed_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_stripe_events_type
            ON stripe_events(type);
        CREATE INDEX IF NOT EXISTS idx_stripe_events_received
            ON stripe_events(received_at);
        """,
    ),
    (
        "010",
        "jobs.recovery_count (restart recovery) + list/pagination indexes",
        # Python migration (see _migration_010). Gated by the
        # schema_migrations ledger like every other entry.
        "python:010",
    ),
]


# ── schema_migrations ledger (F-11) ───────────────────────────
#
# Every migration id is recorded in `schema_migrations` in the SAME
# transaction that applies it, so a migration runs exactly once per
# database. The ledger transaction is opened with BEGIN IMMEDIATE:
# a second process (or thread) that boots at the same time blocks on
# SQLite's write lock, then re-reads the ledger and finds nothing left
# to do. `Repo._bootstrap_schema` additionally holds a cross-process
# bootstrap lock around the whole bootstrap (see core/repo.py).

LEDGER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "id TEXT PRIMARY KEY, "
    "description TEXT NOT NULL, "
    "applied_at TEXT NOT NULL)"
)

# Busy timeout (ms) used while the ledger transaction waits for a
# concurrent booter to finish. Restored to the connection default
# (sqlite3.connect timeout=5.0 -> 5000 ms) afterwards.
MIGRATION_BUSY_TIMEOUT_MS = 60_000
_DEFAULT_BUSY_TIMEOUT_MS = 5_000


def _now_iso() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _split_sql(sql: str) -> List[str]:
    """Split a SQL script into complete statements.

    `executescript` cannot be used inside the ledger transaction
    because it issues an implicit COMMIT first, so each statement is
    executed individually. `sqlite3.complete_statement` handles
    semicolons inside string literals and trigger bodies.
    """
    import sqlite3
    out: List[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt and stmt != ";":
                out.append(stmt)
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def _columns(conn, table: str) -> set:
    return {r[1].lower() for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migration_010(conn) -> None:
    """jobs.recovery_count + indexes for keyset pagination.

    The ALTER is guarded by PRAGMA table_info as well as the ledger,
    so a DB whose ledger was lost/restored separately still boots.
    """
    if "recovery_count" not in _columns(conn, "jobs"):
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN recovery_count "
            "INTEGER NOT NULL DEFAULT 0"
        )
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_jobs_workspace_created "
        "ON jobs(workspace_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_state_updated "
        "ON jobs(state, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_assets_workspace_uploaded "
        "ON assets(workspace_id, uploaded_at)",
        "CREATE INDEX IF NOT EXISTS idx_publish_job_created "
        "ON publish_intents(job_id, created_at)",
        # audit_log(workspace_id, created_at) is indexed by
        # Repo._extend_audit_log_columns: on a fresh DB the
        # workspace_id column is only added after the ledger runs.
    ):
        conn.execute(stmt)


PYTHON_MIGRATIONS: dict = {
    "python:010": _migration_010,
}


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent


def load_migrations() -> List[Tuple[str, str, str]]:
    """Return the ordered list of migrations to apply.

    Kept as a function so tests can monkey-patch / override the
    list. The default is the in-process MIGRATIONS table.
    """
    return list(MIGRATIONS)


def applied_migration_ids(conn) -> List[str]:
    """Return the ids recorded in the ledger (empty if no ledger)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='schema_migrations'"
    ).fetchone()
    if not row:
        return []
    return [r[0] for r in conn.execute(
        "SELECT id FROM schema_migrations ORDER BY id"
    ).fetchall()]


def _apply_one(conn, sql: str) -> None:
    fn = PYTHON_MIGRATIONS.get(sql.strip())
    if fn is not None:
        fn(conn)
        return
    for stmt in _split_sql(sql):
        conn.execute(stmt)


def apply_migrations(conn, *, log: Callable[[str], None] = lambda _msg: None) -> int:
    """Apply all pending migrations to an open SQLite connection.

    Each migration id is applied at most once per database: the id is
    written to `schema_migrations` in the same transaction as the
    migration itself. Re-running is a no-op. Returns the number of
    migrations applied by THIS call (0 when the ledger is complete).

    The connection must be in autocommit mode (``isolation_level=None``,
    which is how ``core.cipher_db.connect`` opens it).
    """
    conn.execute(LEDGER_SCHEMA)
    conn.execute(f"PRAGMA busy_timeout={MIGRATION_BUSY_TIMEOUT_MS}")
    applied = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            done = set(applied_migration_ids(conn))
            for mid, desc, sql in load_migrations():
                if mid in done:
                    continue
                log(f"migration {mid}: applying")
                _apply_one(conn, sql)
                conn.execute(
                    "INSERT INTO schema_migrations(id, description, "
                    "applied_at) VALUES (?,?,?)",
                    (mid, desc, _now_iso()),
                )
                applied += 1
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute(f"PRAGMA busy_timeout={_DEFAULT_BUSY_TIMEOUT_MS}")
    return applied
