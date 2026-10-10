"""Operator maintenance commands.

    python -m cli.maintenance retention [--dry-run]
    python -m cli.maintenance migrations

``retention``: one sweep of ARTIFACTS_RETENTION_DAYS (job artifacts
under VIDEO_FACTORY_STORAGE/jobs) and AUDIT_LOG_RETENTION_DAYS
(audit_log + runner_audit rows). Both default to 0 = off. Prints a JSON
summary. Meant for a systemd timer or a compose cron (see
docs/operations-prod.md "Retention").

``migrations``: print the schema_migrations ledger (applied ids) and
the ids still pending. Opening the Repo applies pending migrations,
exactly like the API does on boot.

Exit code 0 on success, 1 when a sweep half reported an error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _db_path() -> str:
    return os.environ.get(
        "VIDEO_FACTORY_DB", str(_ROOT / "artifacts" / "video_factory.db"),
    )


def _storage_root() -> str:
    return os.environ.get(
        "VIDEO_FACTORY_STORAGE", str(_ROOT / "artifacts" / "storage"),
    )


def _cmd_retention(args: argparse.Namespace) -> int:
    from core.repo import Repo
    from core.retention import run_retention
    repo = Repo(db_path=args.db or _db_path())
    result = run_retention(repo=repo, storage_root=args.storage or _storage_root(),
                           dry_run=args.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    failed = any("error" in (v or {}) for v in result.values())
    return 1 if failed else 0


def _cmd_migrations(args: argparse.Namespace) -> int:
    from core.migrations import applied_migration_ids, load_migrations
    from core.repo import Repo
    repo = Repo(db_path=args.db or _db_path())
    applied = applied_migration_ids(repo._conn())  # noqa: SLF001
    pending = [m[0] for m in load_migrations() if m[0] not in set(applied)]
    print(json.dumps({"applied": applied, "pending": pending}, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m cli.maintenance")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("retention", help="one retention sweep")
    r.add_argument("--dry-run", action="store_true",
                   help="count what would be deleted, delete nothing")
    r.add_argument("--db", default=None, help="override VIDEO_FACTORY_DB")
    r.add_argument("--storage", default=None,
                   help="override VIDEO_FACTORY_STORAGE")
    r.set_defaults(func=_cmd_retention)
    m = sub.add_parser("migrations", help="show schema_migrations ledger")
    m.add_argument("--db", default=None, help="override VIDEO_FACTORY_DB")
    m.set_defaults(func=_cmd_migrations)
    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
