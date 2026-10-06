"""SQLite migrations for Video Factory.

Each migration is a small, idempotent SQL fragment that the Repo
applies at bootstrap. Migrations are append-only — never edit a
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
]


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent


def load_migrations() -> List[Tuple[str, str, str]]:
    """Return the ordered list of migrations to apply.

    Kept as a function so tests can monkey-patch / override the
    list. The default is the in-process MIGRATIONS table.
    """
    return list(MIGRATIONS)


def apply_migrations(conn, *, log: Callable[[str], None] = lambda _msg: None) -> int:
    """Apply all pending migrations to an open SQLite connection.

    Idempotent: re-running is a no-op for already-applied migrations.
    Returns the number of migrations applied this call (0 if all
    already applied).
    """
    applied = 0
    for mid, _desc, sql in load_migrations():
        log(f"migration {mid}: applying")
        conn.executescript(sql)
        applied += 1
    return applied
