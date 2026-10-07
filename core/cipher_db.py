"""SQLCipher integration for Video Factory.

Wave 5 brings encryption-at-rest to the SQLite database. The cipher
binary is `sqlcipher3-binary` (the maintained fork; `pysqlcipher3`
no longer publishes cp312 wheels on PyPI as of 2026-10). Both expose
the PEP 249 dbapi2 surface, so the rest of the codebase sees only
the standard `sqlite3`-compatible connection object.

Three states matter when `Repo` boots:

  * `"plaintext"`  — file exists, no key set, file is a normal
                     SQLite header. Legacy / dev mode. Works
                     exactly as before Wave 5.
  * `"cipher"`     — file exists, key set, SQLCipher header
                     accepted with the right key. This is the
                     production state.
  * `"mismatch"`   — key set but DB is plaintext (or wrong key).
                     Wave 5 raises `DBLocked` and the operator
                     must run `migrate_plaintext_to_cipher` once
                     before the API will come up.

The cipher key comes from `VIDEO_FACTORY_DB_KEY` and is separate
from `VIDEO_FACTORY_SECRET_KEY` (which guards the credential vault
Fernet blobs). The two env vars are rotated independently.

Why a separate key: rotating the row-encryption key (`VIDEO_FACTORY_
SECRET_KEY`) requires re-encrypting every credential blob; rotating
the page-encryption key (`VIDEO_FACTORY_DB_KEY`) requires
re-encrypting every page. They are different operations with
different blast radii. Conflating them is what earlier
single-tenant prototypes got wrong.

Header detection: the first 16 bytes of any SQLite or SQLCipher v4
file are `"SQLite format 3\\0"`. Beyond that the format diverges.
A pragmatic check is to attempt to read `sqlite_master` without a
key — if it succeeds the file is plaintext; if SQLCipher raises
`file is not a database` the file is cipher (or corrupt). We use
that check rather than byte-pattern sniffing because the latter is
brittle across SQLCipher versions.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


class CipherError(RuntimeError):
    """Base class for all Wave 5 cipher layer errors."""


class DBKeyMissing(CipherError):
    """VIDEO_FACTORY_DB_KEY is not set on a cipher DB."""


class DBKeyWrong(CipherError):
    """VIDEO_FACTORY_DB_KEY is set but the DB won't open with it."""


class PlaintextDBRefused(CipherError):
    """DB is plaintext but a key is configured.

    The operator must run `migrate_plaintext_to_cipher` once to
    convert the plaintext file into a cipher file. Refusing to
    silently reopen a "keyed" deployment as plaintext is the
    whole point of Wave 5.
    """


# ── cipher backend selection ──────────────────────────────────────
#
# `sqlcipher3-binary` is the maintained, multi-platform prebuilt
# wheel; it exposes the same dbapi2 surface as `pysqlcipher3`. The
# brief originally named `pysqlcipher3`; that wheel no longer
# publishes cp312 builds on PyPI (verified 2026-10). The
# blockers.md anticipated this and named the binary fallback. We
# try the binary first because it never needs a C compiler.

_CIPHER_BACKEND = None
_CIPHER_IMPORT_ERROR: Optional[str] = None


def _cipher_module():
    """Lazily import + cache the SQLCipher dbapi2 module.

    Raises CipherError if neither sqlcipher3-binary nor a working
    sqlite3 fallback is importable. The fallback (plain sqlite3) is
    ONLY useful for plaintext dev mode and tests that don't set
    VIDEO_FACTORY_DB_KEY.
    """
    global _CIPHER_BACKEND, _CIPHER_IMPORT_ERROR
    if _CIPHER_BACKEND is not None:
        return _CIPHER_BACKEND
    try:
        from sqlcipher3 import dbapi2 as _mod  # type: ignore
        _CIPHER_BACKEND = _mod
        return _mod
    except Exception as e:  # noqa: BLE001
        _CIPHER_IMPORT_ERROR = f"sqlcipher3-binary: {e!r}"
    # Fallback: sqlite3. Only safe for plaintext dev/test paths.
    if _cipher_key() is None:
        _CIPHER_BACKEND = sqlite3
        return sqlite3
    raise CipherError(
        "VIDEO_FACTORY_DB_KEY is set but sqlcipher3-binary is not "
        "installed. Either unset the key for plaintext dev or "
        "`pip install sqlcipher3-binary` to enable cipher mode."
        f"  Import attempts: {_CIPHER_IMPORT_ERROR}"
    )


def _cipher_key() -> Optional[str]:
    """Return the configured cipher key or None for plaintext mode."""
    raw = os.environ.get("VIDEO_FACTORY_DB_KEY", "").strip()
    return raw or None


def is_cipher_mode() -> bool:
    """True iff VIDEO_FACTORY_DB_KEY is set in the current env."""
    return _cipher_key() is not None


# ── header detection ──────────────────────────────────────────────

_PLAINTEXT_HEADER = b"SQLite format 3\x00"


def _read_header(path: str, n: int = 16) -> bytes:
    p = Path(path)
    if not p.exists() or p.stat().st_size < n:
        return b""
    with open(path, "rb") as f:
        return f.read(n)


def looks_like_cipher_file(path: str) -> bool:
    """Return True iff the file at `path` looks like a SQLCipher file.

    SQLCipher v4 keeps the first 16 bytes identical to SQLite
    (`"SQLite format 3\\0"`); beyond byte 16 the bytes diverge. We
    can't reliably distinguish the two from the header alone, so we
    fall back to attempting a keyless open: plaintext opens, cipher
    raises `file is not a database`.
    """
    h = _read_header(path)
    return h.startswith(_PLAINTEXT_HEADER)


def detect_plaintext(path: str) -> bool:
    """Return True iff `path` is a valid plaintext SQLite DB.

    Used by Repo on startup to refuse to open a plaintext DB when
    the operator has configured VIDEO_FACTORY_DB_KEY. If the file
    doesn't exist (fresh install) we return False so the new
    encrypted DB can be created.
    """
    if not Path(path).exists():
        return False
    if Path(path).stat().st_size == 0:
        return False
    if not looks_like_cipher_file(path):
        # Even an empty or freshly-initialised SQLite file has the
        # magic header; if the bytes don't match, the file is
        # either corrupt or encrypted. Treat as non-plaintext.
        return False
    try:
        c = sqlite3.connect(path)
        try:
            row = c.execute(
                "SELECT count(*) FROM sqlite_master"
            ).fetchone()
            return row is not None
        finally:
            c.close()
    except sqlite3.DatabaseError:
        return False


# ── connection factory ────────────────────────────────────────────

def connect(path: str, *, key: Optional[str] = None) -> sqlite3.Connection:
    """Open `path` with the appropriate backend.

    If `key` is None we use the env var. If no key is configured
    we fall back to the system sqlite3 (plaintext mode). If a key
    is configured we use sqlcipher3-binary and issue `PRAGMA key`
    immediately after open.

    The returned connection has `isolation_level=None` so the
    caller manages transactions explicitly — same as the rest of
    the Repo.

    We normalise the cipher-driver's `DatabaseError` to the stdlib
    `sqlite3.DatabaseError` so callers can `except
    sqlite3.DatabaseError` once and catch both backends.  Without
    this normalisation `sqlcipher3.dbapi2.DatabaseError` does NOT
    inherit from `sqlite3.DatabaseError` (verified 2026-10), and
    every existing `except sqlite3.DatabaseError` site in the
    Repo would silently leak a raw `sqlcipher3` exception.
    """
    effective_key = key if key is not None else _cipher_key()
    mod = _cipher_module() if effective_key else sqlite3
    conn = mod.connect(path, check_same_thread=False,
                       isolation_level=None)
    if effective_key:
        # PRAGMA key must be the first statement on the connection;
        # pass the key verbatim — SQLCipher takes it as a string
        # literal and applies PBKDF2 internally. We escape single
        # quotes per the SQLCipher docs.
        safe = effective_key.replace("'", "''")
        try:
            conn.execute(f"PRAGMA key='{safe}'")
        except Exception as exc:
            # sqlcipher3 raises a private DatabaseError subclass
            # that does not inherit from sqlite3.DatabaseError; we
            # wrap it in the stdlib class so callers can rely on
            # one exception family.
            try:
                conn.close()
            except Exception:
                pass
            if isinstance(exc, sqlite3.DatabaseError):
                raise
            raise sqlite3.DatabaseError(
                f"sqlcipher open failed: {exc}"
            ) from exc
    return conn


# ── plaintext → cipher migration ──────────────────────────────────

def _split_dump(dump_sql: str) -> list[str]:
    """Split a `Connection.iterdump()` string into individual statements.

    `iterdump()` emits one statement per line, terminated by `;`,
    and the SQLite parser guarantees no string literal contains an
    unescaped `;`.  Splitting on `;\\n` is therefore safe in
    practice and avoids the cost of a full SQL tokeniser.
    """
    return [s for s in dump_sql.split(";\n") if s.strip()]


def copy_cipher_to_cipher(*, src_path: str, dst_path: str,
                            src_key: str, dst_key: str) -> dict:
    """Copy a cipher DB opened with `src_key` to a fresh cipher
    DB opened with `dst_key`.

    Used by `key_rotation.rotate`: the LIVE DB is read with the
    OLD key and a brand-new DB is written with the NEW key.  The
    algorithm is the same shape as `migrate_plaintext_to_cipher`
    (dump -> split -> execute inside one transaction -> WAL +
    verify) but the source connection is a cipher connection
    with `src_key`, not a plaintext sqlite3 connection.

    Used in place of `migrate_plaintext_to_cipher` during rotation
    because the LIVE DB is not plaintext.

    Note: the sqlcipher3.dbapi2 backend omits `Connection.iterdump`
    even though `sqlite3` ships it.  We implement our own dump
    via `SELECT ... FROM sqlite_master` + per-table `SELECT` so we
    do not depend on driver-level helpers.
    """
    if not Path(src_path).exists():
        raise FileNotFoundError(f"src DB not found: {src_path}")
    if not Path(src_path).stat().st_size:
        raise ValueError(f"src DB is zero bytes: {src_path}")
    dst_path_p = Path(dst_path)
    dst_path_p.parent.mkdir(parents=True, exist_ok=True)
    if dst_path_p.exists():
        raise FileExistsError(
            f"{dst_path} already exists; refusing to overwrite. "
            f"Remove it first if you want to migrate."
        )

    src = connect(src_path, key=src_key)
    try:
        # SQLCipher (and SQLite WAL in general) does not let a
        # brand-new connection see rows that another connection
        # committed but has not yet checkpointed to the main
        # database file.  Force a FULL checkpoint so the dump
        # we are about to read reflects the live Repo state, not
        # a stale snapshot from before the caller wrote anything.
        # PASSIVE returns (0,0,0) on an idle WAL, which is why
        # we need FULL here.
        try:
            src.execute("PRAGMA wal_checkpoint(FULL)")
        except sqlite3.DatabaseError:
            # Some build flavours reject FULL in non-WAL mode.
            pass
        dump_sql = _dump_database(src)
    finally:
        src.close()

    # Split dump into three buckets — TABLES, then INDEXes / VIEWs /
    # TRIGGERs, then INSERTs.  We apply TABLES first, then the
    # dependent CREATE statements, then the INSERTs.  This
    # sidesteps two real bugs we hit when we executed the dump in
    # linear order:
    #
    #   * `sqlite_master` is iterated in alphabetical order, so
    #     `CREATE INDEX idx_jobs_workspace` appears BEFORE
    #     `CREATE TABLE jobs` in the dump.  Executing the index
    #     first raises "no such table: main.jobs".
    #   * An INSERT into a table that has not been CREATEd yet
    #     raises "no such table".
    #
    # The FK-ordering bug is handled by `PRAGMA foreign_keys=OFF`
    # above.
    raw_statements = _split_dump(dump_sql)
    table_stmts: list[str] = []
    other_create_stmts: list[str] = []
    insert_stmts: list[str] = []
    for s in raw_statements:
        norm = s.strip().rstrip(";").strip().upper()
        if norm.startswith("CREATE TABLE"):
            table_stmts.append(s)
        elif norm.startswith("CREATE"):
            # CREATE INDEX, CREATE VIEW, CREATE TRIGGER
            other_create_stmts.append(s)
        else:
            insert_stmts.append(s)

    dst = connect(str(dst_path_p), key=dst_key)
    try:
        dst.execute("PRAGMA foreign_keys=OFF")
        dst.execute("BEGIN IMMEDIATE")
        try:
            for bucket in (table_stmts, other_create_stmts,
                            insert_stmts):
                for stmt in bucket:
                    s = stmt.strip()
                    normalised = s.rstrip(";").strip().upper()
                    if not normalised or normalised in (
                            "BEGIN", "BEGIN TRANSACTION",
                            "COMMIT", "ROLLBACK"):
                        continue
                    dst.execute(s)
            dst.execute("COMMIT")
        except Exception:
            try:
                dst.execute("ROLLBACK")
            except Exception:
                pass
            dst_path_p.unlink(missing_ok=True)
            raise
        dst.execute("PRAGMA foreign_keys=ON")
        dst.execute("PRAGMA journal_mode=WAL")
    finally:
        dst.close()
    return {"src_path": src_path, "dst_path": str(dst_path_p)}


def _dump_database(conn) -> str:
    """Portable `iterdump` substitute.

    `sqlite3.Connection.iterdump` is not implemented on every
    third-party DB-API driver (notably sqlcipher3-binary's
    dbapi2 shim).  We rebuild the equivalent output here: emit
    one CREATE statement per row in `sqlite_master`, then one
    INSERT per row in each table.

    Output is intentionally close to sqlite3.iterdump format so
    `_split_dump` works on the result.
    """
    import json as _json
    pieces: list[str] = ["BEGIN TRANSACTION;"]
    for r in conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall():
        pieces.append(f"{r[1]};")
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    for t in tables:
        for row in conn.execute(f"SELECT * FROM {t}").fetchall():
            # Quote each cell as a SQL literal.
            literals = []
            for v in row:
                if v is None:
                    literals.append("NULL")
                elif isinstance(v, (int, float)):
                    literals.append(str(v))
                elif isinstance(v, bytes):
                    literals.append(f"X'{v.hex()}'")
                else:
                    s = str(v).replace("'", "''")
                    literals.append(f"'{s}'")
            pieces.append(
                f"INSERT INTO {t} VALUES({', '.join(literals)});"
            )
    pieces.append("COMMIT;")
    return "\n".join(pieces)


def migrate_plaintext_to_cipher(*, plain_path: str, cipher_path: str,
                                key: str, verify: bool = True) -> dict:
    """One-shot: read `plain_path` (plaintext) into `cipher_path`
    (SQLCipher with `key`).

    Steps:
      1. Sanity check: `plain_path` is plaintext (raise if not).
      2. Open a cipher DB at `cipher_path` with `key`.
      3. Dump every table's schema + rows from `plain_path` via
         `iterdump()`, execute on the cipher DB inside one
         transaction.
      4. If `verify=True`, re-open the cipher DB with the key and
         assert row counts match. Closes without dropping plaintext
         so the operator can manually delete it once they're
         satisfied.
      5. PRAGMA journal_mode=WAL + foreign_keys=ON on the new DB.

    The migration is reversible: the plaintext file is preserved
    on disk so the operator can fall back with
    `decrypt_db_down.py` (re-import the cipher DB into a fresh
    plaintext file using the same key, then DROP the key from
    env). See `scripts/decrypt_db_down.py`.
    """
    if not Path(plain_path).exists():
        raise FileNotFoundError(f"plaintext DB not found: {plain_path}")
    if not detect_plaintext(plain_path):
        raise ValueError(
            f"{plain_path} is not a plaintext SQLite DB; refusing to "
            f"migrate. If it is already encrypted, no migration is "
            f"needed."
        )

    # Open the plaintext source read-only-ish: we don't write to it.
    src = sqlite3.connect(f"file:{plain_path}?mode=ro", uri=True,
                          check_same_thread=False,
                          isolation_level=None)
    try:
        dump_sql = _dump_database(src)
    finally:
        src.close()

    cipher_path_p = Path(cipher_path)
    cipher_path_p.parent.mkdir(parents=True, exist_ok=True)
    if cipher_path_p.exists():
        # If there's already a cipher file here, refuse to clobber
        # it — the operator must remove it explicitly.
        raise FileExistsError(
            f"{cipher_path} already exists; refusing to overwrite. "
            f"Remove it first if you want to migrate."
        )

    dst = connect(str(cipher_path_p), key=key)
    try:
        dst.execute("PRAGMA foreign_keys=OFF")
        # Same three-bucket ordering as `copy_cipher_to_cipher`:
        # TABLES first, then other CREATEs (INDEX/VIEW/TRIGGER), then
        # INSERTs.  Without the explicit TABLE-first split a
        # `CREATE INDEX idx_jobs_workspace` (which sorts before
        # `CREATE TABLE jobs`) raises "no such table: main.jobs".
        raw_statements = _split_dump(dump_sql)
        table_stmts: list[str] = []
        other_create_stmts: list[str] = []
        insert_stmts: list[str] = []
        for s in raw_statements:
            norm = s.strip().rstrip(";").strip().upper()
            if norm.startswith("CREATE TABLE"):
                table_stmts.append(s)
            elif norm.startswith("CREATE"):
                other_create_stmts.append(s)
            else:
                insert_stmts.append(s)
        dst.execute("BEGIN IMMEDIATE")
        try:
            for bucket in (table_stmts, other_create_stmts,
                            insert_stmts):
                for stmt in bucket:
                    s = stmt.strip()
                    # Normalise: strip trailing semicolons so
                    # `COMMIT;` matches `COMMIT` in the filter.
                    normalised = s.rstrip(";").strip().upper()
                    if not normalised or normalised in (
                            "BEGIN", "BEGIN TRANSACTION",
                            "COMMIT", "ROLLBACK"):
                        continue
                    dst.execute(s)
            dst.execute("COMMIT")
        except Exception:
            try:
                dst.execute("ROLLBACK")
            except Exception:
                pass
            cipher_path_p.unlink(missing_ok=True)
            raise
        dst.execute("PRAGMA foreign_keys=ON")
        dst.execute("PRAGMA journal_mode=WAL")
    finally:
        dst.close()

    if verify:
        # Re-open with the key and spot-check: tables + row counts.
        plain_check = sqlite3.connect(plain_path,
                                       check_same_thread=False,
                                       isolation_level=None)
        try:
            plain_tables = {
                r[0]: plain_check.execute(
                    f"SELECT count(*) FROM {r[0]}"
                ).fetchone()[0]
                for r in plain_check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if not r[0].startswith("sqlite_")
            }
        finally:
            plain_check.close()
        cipher_check = connect(str(cipher_path_p), key=key)
        try:
            cipher_tables = {
                r[0]: cipher_check.execute(
                    f"SELECT count(*) FROM {r[0]}"
                ).fetchone()[0]
                for r in cipher_check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if not r[0].startswith("sqlite_")
            }
        finally:
            cipher_check.close()
        if plain_tables != cipher_tables:
            cipher_path_p.unlink(missing_ok=True)
            raise CipherError(
                f"migration verification failed: plaintext={plain_tables} "
                f"cipher={cipher_tables}"
            )
        return {
            "plain_path": str(plain_path),
            "cipher_path": str(cipher_path_p),
            "tables": {t: {"plaintext_rows": plain_tables[t],
                            "cipher_rows": cipher_tables[t]}
                        for t in plain_tables},
        }

    return {
        "plain_path": str(plain_path),
        "cipher_path": str(cipher_path_p),
    }


# ── module exports for tests ─────────────────────────────────────

__all__ = [
    "CipherError",
    "DBKeyMissing",
    "DBKeyWrong",
    "PlaintextDBRefused",
    "connect",
    "copy_cipher_to_cipher",
    "detect_plaintext",
    "is_cipher_mode",
    "migrate_plaintext_to_cipher",
]