"""Independent migration probe — verify migration 008 adds current_period_end."""
import os
from cryptography.fernet import Fernet
import pytest


def test_migration_008_legacy_db_adds_current_period_end(tmp_path):
    """Simulate a pre-Wave-6a legacy DB: workspaces has stripe_customer_id
    + stripe_subscription_id but NOT current_period_end. Migration 008
    must add it idempotently.
    """
    import sqlite3
    db = str(tmp_path / "legacy.db")
    # Build a pre-Wave-6a workspaces table (no current_period_end)
    conn = sqlite3.connect(db)
    conn.executescript("""
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
    """)
    conn.execute(
        "INSERT INTO workspaces(workspace_id, slug, name, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("wks_legacy", "legacy", "Legacy", "2024-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    # Now open via Repo (which runs migrations 001-008)
    from core.repo import Repo
    repo = Repo(db)
    # Verify the column was added
    cols = {row[1].lower()
            for row in repo._conn().execute(
                "PRAGMA table_info(workspaces)"
            ).fetchall()}
    assert "current_period_end" in cols, (
        f"migration 008 did not add current_period_end; cols: {cols}"
    )
    # Verify existing row still readable
    ws = repo.get_workspace("wks_legacy")
    assert ws["slug"] == "legacy"
    assert "current_period_end" in ws
    assert ws["current_period_end"] is None

    # Reopening should be idempotent (no error)
    repo.close()
    repo2 = Repo(db)
    cols2 = {row[1].lower()
             for row in repo2._conn().execute(
                 "PRAGMA table_info(workspaces)"
             ).fetchall()}
    assert "current_period_end" in cols2
    repo2.close()
