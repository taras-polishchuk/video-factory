#!/usr/bin/env python3
"""Consistent online snapshot of the Video Factory DB (SQLite / SQLCipher).

Copying a live WAL-mode database file can capture a torn state. This
uses SQLite's online backup API through core.cipher_db.connect, so the
snapshot is transactionally consistent and stays encrypted with the
same VIDEO_FACTORY_DB_KEY (pages are never written in plaintext).

Usage (inside the api container, as scripts/backup.sh does):
  python /app/scripts/db_snapshot.py --stdout > video_factory.db
  python /app/scripts/db_snapshot.py --dst /data/tmp/snap.db
Options:
  --src PATH   source DB (default: $VIDEO_FACTORY_DB)

The snapshot is checked with PRAGMA quick_check before it is emitted.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.cipher_db import connect  # noqa: E402


def snapshot(src: str, dst: str) -> None:
    if not Path(src).is_file():
        raise SystemExit(f"ERROR: source DB {src} does not exist")
    if Path(dst).exists():
        raise SystemExit(f"ERROR: {dst} already exists; refusing to overwrite")
    s = connect(src)
    try:
        d = connect(dst)
        try:
            s.backup(d)
            row = d.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise SystemExit(f"ERROR: snapshot quick_check failed: {row!r}")
        finally:
            d.close()
    finally:
        s.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Online DB snapshot")
    ap.add_argument("--src", default=os.environ.get("VIDEO_FACTORY_DB", ""))
    out = ap.add_mutually_exclusive_group(required=True)
    out.add_argument("--dst")
    out.add_argument("--stdout", action="store_true")
    args = ap.parse_args(argv)
    if not args.src:
        print("ERROR: --src or VIDEO_FACTORY_DB is required", file=sys.stderr)
        return 2
    if args.dst:
        snapshot(args.src, args.dst)
        return 0
    with tempfile.TemporaryDirectory(prefix="vf-dbsnap-") as tmp:
        dst = os.path.join(tmp, "snapshot.db")
        snapshot(args.src, dst)
        with open(dst, "rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
