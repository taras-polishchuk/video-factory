"""Reversible: cipher -> plaintext (down-migration).

Wave 5 §3.3 — the brief requires "Reversible migration: ship
`decrypt_db_down.sql` or Python equivalent."  This script is the
Python equivalent.

Usage:

    VIDEO_FACTORY_DB_KEY=<oldkey> python scripts/decrypt_db_down.py

This is the operator's escape hatch if they need to roll back a
cipher upgrade, restore a backup on a machine without the
SQLCipher binary, or copy the DB into an offline forensic tool
that only reads plaintext SQLite.

The script dumps every row from the cipher DB and writes a new
plaintext file next to it (suffix `.plain.db`).  The original
cipher file is preserved.

Verification: after writing the plaintext file, the script
re-opens it without a key and asserts every table's row count
matches the cipher DB.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def _run() -> int:
    from core.cipher_db import connect, _dump_database

    key = os.environ.get("VIDEO_FACTORY_DB_KEY", "").strip()
    if not key:
        print(
            "ERROR: VIDEO_FACTORY_DB_KEY is not set.  Cannot decrypt "
            "without the original cipher key.",
            file=sys.stderr,
        )
        return 2

    cipher = os.environ.get(
        "VIDEO_FACTORY_DB",
        str(Path(__file__).resolve().parent.parent
            / "artifacts" / "video_factory.db"),
    )
    if not Path(cipher).exists():
        print(f"ERROR: {cipher} does not exist.", file=sys.stderr)
        return 2

    plain = Path(cipher).with_suffix(".plain.db")
    if plain.exists():
        print(
            f"ERROR: {plain} already exists.  Refusing to overwrite.  "
            f"Remove it first if you want to re-export.",
            file=sys.stderr,
        )
        return 2

    src = connect(cipher, key=key)
    try:
        dump = _dump_database(src)
    finally:
        src.close()

    # Decode the ciphertext rows so they are valid SQLite strings
    # when written into plaintext.  iterdump produces correctly
    # quoted SQL — no extra escaping needed.
    dst = sqlite3.connect(str(plain), isolation_level=None)
    try:
        statements = [s for s in dump.split(";\n") if s.strip()]
        dst.execute("PRAGMA foreign_keys=OFF")
        for s in statements:
            s2 = s.strip()
            norm = s2.rstrip(";").strip().upper()
            if not norm or norm in ("BEGIN", "BEGIN TRANSACTION",
                                      "COMMIT", "ROLLBACK"):
                continue
            dst.execute(s2)
        dst.execute("PRAGMA foreign_keys=ON")
        dst.execute("PRAGMA journal_mode=WAL")
    finally:
        dst.close()

    # Verify
    src2 = connect(cipher, key=key)
    try:
        tables = [r[0] for r in src2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() if not r[0].startswith("sqlite_")]
        cipher_counts = {t: src2.execute(
            f"SELECT count(*) FROM {t}"
        ).fetchone()[0] for t in tables}
    finally:
        src2.close()
    check = sqlite3.connect(str(plain), isolation_level=None)
    try:
        plain_counts = {t: check.execute(
            f"SELECT count(*) FROM {t}"
        ).fetchone()[0] for t in tables}
    finally:
        check.close()
    if cipher_counts != plain_counts:
        plain.unlink(missing_ok=True)
        print(f"FAILED: row counts diverged: {cipher_counts} vs {plain_counts}",
              file=sys.stderr)
        return 1

    print("OK")
    for t in tables:
        print(f"  {t}: {cipher_counts[t]} rows")
    print()
    print(f"Plaintext export at {plain}.  Cipher original preserved at "
          f"{cipher}.  To return to cipher mode: set VIDEO_FACTORY_DB_KEY "
          f"and restart; the cipher DB is still on disk.")
    return 0


if __name__ == "__main__":
    sys.exit(_run())