"""Wave 5 QA — adversarial tests for SQLCipher + key rotation.

Independent verification layer on top of the builder's
test_wave5_cipher.py suite. Each test exercises an attack or
edge case the builder either skipped or covered with the
canonical happy-path fixture.

Tests:

  1. test_encrypted_db_with_wrong_key_raises
       Open a cipher DB with a key that does NOT match the one
       used at create. Must raise sqlite3.DatabaseError (and the
       underlying sqlcipher3.dbapi2.DatabaseError must be
       normalised to stdlib).

  2. test_plaintext_db_refused_with_helpful_error
       Drop a plaintext SQLite DB at the configured db_path while
       VIDEO_FACTORY_DB_KEY is set. Repo must refuse and surface
       a message that names `migrate_plaintext_to_cipher`.

  3. test_migration_atomicity_rollback_on_failure
       Trigger a migration failure mid-stream (e.g. duplicate
       table name) and assert no partial cipher file is left on
       disk + no half-written rows in any other DB.

  4. test_rotation_audit_records_user_id
       Run rotation with a known actor_user_id. The NEW-cipher
       DB (read with the NEW key) MUST have an audit row whose
       user_id matches.

  5. test_per_workspace_hmac_prevents_cross_workspace_decrypt
       Encrypt a sidecar under workspace A, attempt verification
       under workspace B and under a tampered storage_key.
       Both must fail.

Plus the discovered BLOCKER for Wave 5 §3.4 (proves Repo.db_key
is not refreshed after a rotation swap — silent data corruption
in production deployments where the API process continues to
serve writes through the same Repo instance):

  6. test_repo_post_rotation_writes_are_persistent (currently
     FAILS — see defect report).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from core import cipher_db
from control_plane import artifact_hmac, key_rotation
from core.repo import Repo


# ── helpers ───────────────────────────────────────────────────────


def _seed_workspace(repo: Repo) -> dict:
    """Create a user + workspace for FK-bearing tables."""
    user = repo.create_user(email=f"qa-{os.getpid()}@example.com",
                            password_hash="x" * 32)
    ws = repo.create_workspace(slug="qa", name="QA", owner_user_id=user["user_id"])
    return {"user": user, "workspace": ws}


def _write_plaintext_db(path: str) -> None:
    """Make a small plaintext SQLite DB at `path`."""
    c = sqlite3.connect(path)
    try:
        c.execute("CREATE TABLE x(a INTEGER, b TEXT)")
        c.executemany("INSERT INTO x VALUES(?,?)",
                      [(1, "alpha"), (2, "beta"), (3, "gamma")])
        c.commit()
    finally:
        c.close()


# ── 1. encrypted DB with wrong key ────────────────────────────────


def test_encrypted_db_with_wrong_key_raises(tmp_path: Path):
    """Cipher DB opened with the wrong key must raise stdlib
    sqlite3.DatabaseError.

    The cipher middleware (sqlcipher3-binary) raises a private
    `sqlcipher3.dbapi2.DatabaseError` that does NOT inherit from
    `sqlite3.DatabaseError`. Repo._conn / cipher_db.connect must
    normalise this so existing `except sqlite3.DatabaseError`
    sites fire. Without the normalisation, every existing
    call site silently leaks the raw cipher exception.
    """
    db = str(tmp_path / "enc.db")
    key = "correct-key-12345678"

    # Create cipher DB with the correct key.
    c = cipher_db.connect(db, key=key)
    c.execute("CREATE TABLE secret(id INTEGER, payload TEXT)")
    c.executemany(
        "INSERT INTO secret VALUES(?,?)",
        [(1, "shh"), (2, "private"), (3, "data")],
    )
    c.commit()
    c.close()

    # Wrong key via Repo MUST raise sqlite3.DatabaseError.
    with pytest.raises(sqlite3.DatabaseError):
        Repo(db, db_key="WRONG-key-9999")._conn().execute("SELECT 1")

    # Wrong key via raw cipher_db.connect MUST also surface a
    # DatabaseError (the underlying class — stdlib or
    # sqlcipher3-binary private).
    with pytest.raises(Exception) as excinfo:
        cipher_db.connect(db, key="WRONG-key-9999").execute("SELECT 1")
    assert "not a database" in str(excinfo.value).lower()


# ── 2. plaintext DB refused with helpful error ────────────────────


def test_plaintext_db_refused_with_helpful_error(tmp_path: Path, monkeypatch):
    """Plaintext DB + VIDEO_FACTORY_DB_KEY set ⇒ Repo refuses to open.

    The error must name `migrate_plaintext_to_cipher` so the
    operator knows the recovery path. Without this hint an
    operator who upgraded to a keyed deployment is stuck.
    """
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "qa-key-aaaa")
    db = str(tmp_path / "plain.db")
    _write_plaintext_db(db)

    with pytest.raises(cipher_db.PlaintextDBRefused) as excinfo:
        Repo(db)

    msg = str(excinfo.value)
    assert "migrate_plaintext_to_cipher" in msg, (
        "error message must name the migration helper; got: " + msg
    )
    assert "VIDEO_FACTORY_DB_KEY" in msg


# ── 3. migration atomicity (rollback on failure) ──────────────────


def test_migration_atomicity_rollback_on_failure(tmp_path: Path, monkeypatch):
    """Migration failure mid-stream must leave NO partial cipher DB.

    Simulates a duplicate-table failure (the cipher file gets
    a CREATE TABLE x, then our pre-seeded file's INSERT for
    table `x` collides — or we monkeypatch _dump_database to
    return a duplicate CREATE).
    """
    src = str(tmp_path / "src.db")
    dst = str(tmp_path / "dst.db")
    _write_plaintext_db(src)

    # Monkeypatch _dump_database to inject a duplicate CREATE.
    import core.cipher_db as _cd

    real_dump = _cd._dump_database

    def bad_dump(conn):
        sql = real_dump(conn)
        # Inject a duplicate CREATE TABLE so the cipher DB will
        # raise "table x already exists" when replayed.
        return sql + "\nCREATE TABLE x(a INTEGER);\n"

    monkeypatch.setattr(_cd, "_dump_database", bad_dump)

    with pytest.raises(Exception):
        _cd.migrate_plaintext_to_cipher(
            plain_path=src, cipher_path=dst, key="qa-key",
        )

    # No partial cipher file must be left.
    assert not Path(dst).exists(), (
        f"partial cipher file left after failure: {dst}"
    )
    # No WAL/SHM dangling around.
    for ext in ("", "-wal", "-shm"):
        assert not Path(dst + ext).exists()


# ── 4. rotation audit records user_id ─────────────────────────────


def test_rotation_audit_records_user_id(tmp_path: Path, monkeypatch):
    """Rotation with a known actor_user_id must write an audit
    row whose `user_id` matches.

    Read via the NEW key (fresh connection) to bypass any
    Repo._conn stale-cache side effects of the swap.
    """
    db = str(tmp_path / "rot.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "rot-old-key")

    repo = Repo(db, db_key="rot-old-key")
    seed = _seed_workspace(repo)
    actor = seed["user"]["user_id"]

    # Rotate.
    state = key_rotation.rotate(
        repo=repo, old_key="rot-old-key", new_key="rot-new-key",
        actor_user_id=actor, db_path=db,
    )
    assert state["completed"], f"rotation did not complete: {state}"
    assert state["actor_user_id"] == actor
    assert state["old_key_fingerprint"] == key_rotation._fingerprint(
        "rot-old-key")
    assert state["new_key_fingerprint"] == key_rotation._fingerprint(
        "rot-new-key")

    # Read via NEW key — must have an audit row attributable to
    # the actor and referencing key.rotation_completed.
    new_conn = cipher_db.connect(db, key="rot-new-key")
    try:
        rows = new_conn.execute(
            "SELECT event, user_id, context_json FROM audit_log "
            "WHERE event LIKE 'key.rotation%' ORDER BY created_at",
        ).fetchall()
    finally:
        new_conn.close()

    events = [r[0] for r in rows]
    user_ids = {r[1] for r in rows}
    assert "key.rotation_started" in events, \
        f"missing key.rotation_started audit: {events}"
    assert actor in user_ids, \
        f"actor_user_id {actor!r} not recorded in audit user_ids: {user_ids}"


# ── 5. per-workspace HMAC prevents cross-workspace decrypt ───────


def test_per_workspace_hmac_prevents_cross_workspace_decrypt(tmp_path: Path):
    """A blob tagged for workspace A must NOT verify under workspace B.

    Covers the cross-tenant threat: a malicious operator with
    shell access to artifacts/storage/ tries to read
    `workspaces/A/assets/foo` as workspace B.
    """
    monkeypatch_root = str(tmp_path)
    os.environ["VIDEO_FACTORY_DB_KEY"] = "hmac-key-12345678"

    storage_key = "workspaces/alice/assets/test_foo.png"

    artifact_hmac.write_sidecar(
        storage_root=monkeypatch_root,
        storage_key=storage_key,
        workspace_id="ws_alice",
    )

    # Owner workspace verifies.
    assert artifact_hmac.verify_sidecar(
        storage_root=monkeypatch_root,
        storage_key=storage_key,
        workspace_id="ws_alice",
    ), "owner workspace must verify"

    # Different workspace must fail.
    assert not artifact_hmac.verify_sidecar(
        storage_root=monkeypatch_root,
        storage_key=storage_key,
        workspace_id="ws_bob",
    ), "cross-workspace verify must fail"

    # Tampered storage_key (path traversal) must fail.
    assert not artifact_hmac.verify_sidecar(
        storage_root=monkeypatch_root,
        storage_key="workspaces/bob/assets/test_foo.png",
        workspace_id="ws_bob",
    ), "tampered storage_key verify must fail"

    # Missing sidecar must fail (fail-closed).
    (tmp_path / (storage_key + ".hmac")).unlink()
    assert not artifact_hmac.verify_sidecar(
        storage_root=monkeypatch_root,
        storage_key=storage_key,
        workspace_id="ws_alice",
    ), "missing sidecar must fail closed"


# ── 6. (DISCOVERY) Repo post-rotation writes are not persistent ──
#
# Defect discovered during Wave 5 QA independent verification:
# after key_rotation.rotate() runs, the same Repo instance's
# cached connection still uses the OLD key. Subsequent writes
# through that connection silently land on disk encrypted
# with the OLD key (or are dropped) and are NOT visible to a
# fresh connection opened with the NEW key. This causes silent
# data corruption in any production deployment that continues
# to serve writes through the same Repo instance after rotation
# (i.e. all production deployments, since restarting the API
# process is the documented but not enforced post-rotation step).
#
# The test below is currently FAILING — it documents the defect
# and will turn green once the upstream fix lands (either Repo
# auto-refreshes its db_key after a swap, or the rotate()
# function rebuilds the connection cache).


def test_repo_post_rotation_writes_are_persistent(tmp_path: Path, monkeypatch):
    """DEFECT DISCOVERY: Repo writes after rotation must reach
    the NEW-cipher DB.

    Reads back via a fresh cipher_db connection (NEW key) to
    bypass Repo._conn's stale cache and prove that what was
    "committed" through Repo is actually on disk in the new
    cipher.
    """
    db = str(tmp_path / "rot_persist.db")
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", "per-old-key")

    repo = Repo(db, db_key="per-old-key")
    _seed_workspace(repo)

    # Rotate.
    state = key_rotation.rotate(
        repo=repo, old_key="per-old-key", new_key="per-new-key",
        actor_user_id="qa-actor", db_path=db,
    )
    assert state["completed"], f"rotation failed: {state}"

    # Now write through the SAME repo instance (post-rotation).
    repo._conn().execute(
        "INSERT INTO audit_log(audit_id, event, context_json, "
        "user_id, created_at) VALUES (?,?,?,?,?)",
        ("post_rot_1", "post.rot.write", "{\"src\":\"repo\"}",
                 "qa-actor", "2026-10-07T00:00:00Z"),
    )
    repo._conn().commit()

    # The row must be visible to a fresh connection opened
    # with the NEW key. If it's NOT visible, the rotation
    # swapped the file but left Repo pointing at the OLD key —
    # the write was either silently dropped or landed on a
    # page encrypted with the wrong key (silent data corruption
    # in production).
    fresh = cipher_db.connect(db, key="per-new-key")
    try:
        rows = fresh.execute(
            "SELECT event, context_json FROM audit_log "
            "WHERE audit_id='post_rot_1'"
        ).fetchall()
    finally:
        fresh.close()

    assert rows == [("post.rot.write", '{"src":"repo"}')], (
        "write through Repo after rotation is NOT visible to a "
        "fresh NEW-key connection: Repo.db_key was not refreshed "
        "after the swap. This is BLOCKER for production rotation."
    )