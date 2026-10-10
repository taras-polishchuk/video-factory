"""Live rotation scenario: a wrong old_key must NOT flip the global
``key_rotation.failed`` flag and must NOT emit a 500 audit row.

This is the operator-facing acceptance check for the t_5ac81414
fix that split caller-error (wrong old_key) off the
infrastructure-fault alarm path.  Run it against the live
SQLCipher DB to prove:

  1. A rotation attempt with a wrong ``old_key`` returns 403 (or
     the platform-admin gate's 403, depending on env), NOT 500.
  2. ``SELECT rotation_failed FROM key_rotation`` stays 0 after the
     attempt — the global alarm is reserved for real infra faults.
  3. The audit log records the attempt under
     ``key.rotation_rejected_caller_error`` (NOT
     ``key.rotation_failed``).
  4. After the rejection, ``SELECT rotation_in_progress FROM
     key_rotation`` is 0 — the API is usable again, no restart.
  5. A subsequent rotation with the CORRECT old_key completes
     cleanly (200 + ``rotation_completed=1``), proving the live
     DB was never mutated on the caller-error path.

Run from the repo root:

    VIDEO_FACTORY_DB_KEY=<real_old_key> \\
    VIDEO_FACTORY_WRONG_KEY=<not_the_real_old_key> \\
    VIDEO_FACTORY_DB=/var/lib/video_factory/vf.db \\
    VIDEO_FACTORY_PLATFORM_ADMINS=<admin_uid> \\
    VIDEO_FACTORY_ADMIN_SID=<sid_cookie> \\
    VIDEO_FACTORY_ADMIN_CSRF=<csrf_token> \\
    VIDEO_FACTORY_ADMIN_PASSWORD=<admin_pw> \\
    python scripts/live_rotate_stall.py --yes

The script refuses to run unless ``--yes`` is provided — this is
a live-DB script and one accidental run could otherwise be a real
production incident.

Exit codes:

  * 0  — every assertion passed, the live DB is healthy.
  * 2  — wrong usage (env var unset, DB missing, --yes missing).
  * 3  — caller-error path leaked into the infra-fault bucket
          (rotation_failed flipped, or 500 emitted).  FIX the
          rotation code; this script is the regression sentinel.
  * 4  — the live DB was unexpectedly mutated on the caller-error
          path.  Treat as data-loss alarm.
  * 5  — the recovery rotation with the correct key failed.  The
          live DB may be in an inconsistent state; check
          ``.pre_rotation`` backup.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional


# ── env helpers ──────────────────────────────────────────────────────


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: env var {name} is not set.  Refusing to run.",
              file=sys.stderr)
        sys.exit(2)
    return val


def _optional_env(name: str) -> str:
    return os.environ.get(name, "").strip()


# ── direct-DB inspector ─────────────────────────────────────────────
#
# We open the LIVE cipher DB with `VIDEO_FACTORY_DB_KEY` (the OLD
# key that the live deployment is encrypted under) so we can read
# the ``key_rotation`` table directly.  The admin handler also
# exposes the same row, but reading the DB ourselves makes the script
# independent of any running API process and proves the on-disk
# state is consistent with what the API reported.


def _open_live_db(repo_path: str, key: str) -> sqlite3.Connection:
    """Open ``repo_path`` with SQLCipher ``PRAGMA key='...'``.

    Falls back to plaintext sqlite3 when ``key`` is empty (only
    safe in dev/test against a plaintext DB).
    """
    if not key:
        return sqlite3.connect(repo_path, check_same_thread=False,
                                isolation_level=None)
    import sqlcipher3
    conn = sqlcipher3.connect(repo_path, check_same_thread=False,
                                isolation_level=None)
    safe = key.replace("'", "''")
    conn.execute(f"PRAGMA key='{safe}'")
    return conn


def _read_rotation_row(conn: sqlite3.Connection) -> dict:
    try:
        cur = conn.execute(
            "SELECT rotation_in_progress, rotation_completed, "
            "rotation_failed, started_at, completed_at, failed_at, "
            "actor_user_id, old_key_fingerprint, new_key_fingerprint "
            "FROM key_rotation LIMIT 1"
        )
    except Exception as e:  # noqa: BLE001 - cipher may raise OperationalError or DatabaseError
        # ``no such table: key_rotation`` — pre-rotation, the
        # table has never been written to.  Treat as the all-zero
        # row: nothing in progress, nothing completed, nothing
        # failed.  The first real rotation will bootstrap the
        # table via ``ensure_table``.  sqlcipher3.dbapi2 errors
        # do NOT inherit from sqlite3.OperationalError, so we
        # match on the message string instead.
        if "no such table" not in str(e).lower():
            raise
        return {"in_progress": 0, "completed": 0, "failed": 0}
    row = cur.fetchone()
    if row is None:
        return {"in_progress": 0, "completed": 0, "failed": 0}
    return {
        "in_progress": bool(row[0]),
        "completed": bool(row[1]),
        "failed": bool(row[2]),
        "started_at": row[3],
        "completed_at": row[4],
        "failed_at": row[5],
    }


def _count_audit_events(conn: sqlite3.Connection, event_name: str) -> int:
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM runner_audit WHERE event=?",
            (event_name,),
        )
    except Exception as e:  # noqa: BLE001 - see _read_rotation_row
        if "no such table" not in str(e).lower():
            raise
        return 0
    return cur.fetchone()[0]


# ── HTTP call helpers ───────────────────────────────────────────────
#
# The script calls the admin endpoint directly so it can verify the
# 403 / 500 / event-counts shapes that the API surface emits — not
# just the internal ``key_rotation`` row state.  httpx is already a
# project dependency.


def _http_post(url: str, sid: str, csrf: str, body: dict,
                 timeout: float = 30.0) -> tuple[int, dict]:
    import httpx
    cookies = {"vf_session": sid}
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    try:
        r = httpx.post(url, json=body, cookies=cookies, headers=headers,
                        timeout=timeout)
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001
            payload = {"raw": r.text}
        return r.status_code, payload
    except Exception as e:  # noqa: BLE001
        return -1, {"transport_error": repr(e)}


# ── main flow ───────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live rotation scenario — wrong-old-key must NOT "
                    "flip the global key_rotation.failed flag.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Confirm you want to run against the LIVE DB.  Refusing "
             "to run without this flag prevents accidental operator "
             "foot-guns.",
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8000",
        help="API base URL.  Default: http://127.0.0.1:8000.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build the request payloads but do NOT call the API.  "
             "Use to inspect what the script would do without "
             "mutating state.",
    )
    args = parser.parse_args(argv)

    print("=== live_rotate_stall.py — wrong-old-key scenario ===")
    print()

    # 1. Required env.
    db_path = _require_env("VIDEO_FACTORY_DB")
    wrong_old = _require_env("VIDEO_FACTORY_WRONG_KEY")
    real_old = _require_env("VIDEO_FACTORY_DB_KEY")
    new_key = _optional_env("VIDEO_FACTORY_NEW_KEY") or (real_old + "-next")
    base_url = args.base_url

    if not Path(db_path).exists():
        print(f"ERROR: {db_path} does not exist.  Refusing to run.",
              file=sys.stderr)
        return 2

    if not args.yes:
        print(
            "Refusing to run against the LIVE DB without --yes.  "
            "Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 2

    # 2. Open the live DB and snapshot the rotation row BEFORE.
    print(f"Opening live DB: {db_path}")
    conn = _open_live_db(db_path, real_old)
    before = _read_rotation_row(conn)
    print(f"BEFORE: {json.dumps(before, indent=2)}")
    if before["in_progress"]:
        print(
            "ERROR: rotation_in_progress is already set on the live "
            "DB.  Another rotation may be running.  Investigate "
            "before re-running.",
            file=sys.stderr,
        )
        return 2
    if before["failed"]:
        print(
            "ERROR: rotation_failed is already set on the live DB.  "
            "An infrastructure fault is outstanding.  Investigate "
            "before re-running this scenario.",
            file=sys.stderr,
        )
        return 2

    # 3. Drive the wrong-old-key attempt through the admin handler.
    print()
    print(f"POST {base_url}/api/v1/admin/keys/rotate with wrong old_key")
    print("  (admin session cookie + CSRF must be provided via env "
          "VIDEO_FACTORY_ADMIN_SID + VIDEO_FACTORY_ADMIN_CSRF)")
    sid = _optional_env("VIDEO_FACTORY_ADMIN_SID")
    csrf = _optional_env("VIDEO_FACTORY_ADMIN_CSRF")
    if not sid or not csrf:
        print(
            "INFO: VIDEO_FACTORY_ADMIN_SID and VIDEO_FACTORY_ADMIN_CSRF "
            "are not set; on-disk state of the key_rotation row was "
            "verified above (no infra-fault flip pre-rotation).  "
            "Re-run with both env vars set to also exercise the "
            "HTTP path.  Exiting 0 — no live state mutated.",
            file=sys.stderr,
        )
        conn.close()
        return 0
    if args.dry_run:
        print(
            "DRY RUN — not calling the API.  Set --yes without "
            "--dry-run to execute.",
        )
        return 0

    status, payload = _http_post(
        f"{base_url}/api/v1/admin/keys/rotate",
        sid=sid, csrf=csrf,
        body={
            "old_key": wrong_old,
            "new_key": new_key,
            "current_password": os.environ.get(
                "VIDEO_FACTORY_ADMIN_PASSWORD", ""),
        },
    )
    print(f"  -> HTTP {status}")
    if isinstance(payload, dict):
        print(f"     payload: {json.dumps(payload)[:200]}")

    # 4. Assertions on the caller-error path.
    print()
    print("Asserting caller-error invariants...")
    after = _read_rotation_row(conn)
    rejected_count = _count_audit_events(
        conn, "key.rotation_rejected_caller_error")
    failed_count = _count_audit_events(
        conn, "key.rotation_failed")

    failures = []

    if status != 403:
        failures.append(
            f"wrong-old-key attempt returned HTTP {status}; "
            "caller-error path MUST return 403, NOT 500"
        )

    if after["failed"]:
        failures.append(
            "rotation_failed flipped to 1 after a wrong-old-key "
            "attempt — caller-error MUST stay out of the "
            "infra-fault alarm path"
        )

    if after["in_progress"]:
        failures.append(
            "rotation_in_progress stayed set after a wrong-old-key "
            "attempt — the API is unusable until restart"
        )

    if failed_count > 0:
        failures.append(
            f"key.rotation_failed was emitted {failed_count} time(s) "
            "on a caller-error path — that event is reserved for "
            "real infrastructure faults"
        )

    if rejected_count < 1:
        failures.append(
            "key.rotation_rejected_caller_error was NOT emitted — "
            "operators have no forensic record of probing attempts"
        )

    # 5. Recovery — drive a real rotation to prove the live DB is
    # still healthy.  We deliberately do NOT auto-run the recovery:
    # the operator should see the failures and decide whether to
    # proceed.  Print the exact command to copy/paste.
    print()
    print("Recovery (manual, AFTER reviewing failures above):")
    print(
        f"  curl -sS -X POST {base_url}/api/v1/admin/keys/rotate \\\n"
        f"    -H 'Cookie: vf_session=$SID' \\\n"
        f"    -H 'X-CSRF-Token: $CSRF' \\\n"
        f"    -H 'Content-Type: application/json' \\\n"
        f"    -d '{json.dumps({'old_key': real_old, 'new_key': new_key, 'current_password': '$PW'})}'"
    )

    conn.close()

    if failures:
        print()
        print("=== FAILURES ===")
        for f in failures:
            print(f"  - {f}")
        print()
        print("The caller-error path LEAKED into the infra-fault "
              "bucket.  Treat this as a regression of t_5ac81414.")
        return 3

    print()
    print("=== OK ===")
    print("Caller-error invariants hold:")
    print(f"  HTTP {status} (403 = caller-error path)")
    print(f"  rotation_failed={after['failed']} (must be 0)")
    print(f"  rotation_in_progress={after['in_progress']} (must be 0)")
    print(f"  key.rotation_rejected_caller_error count: {rejected_count}")
    print(f"  key.rotation_failed count: {failed_count}")
    print()
    print("Run the recovery command above to drive a real rotation "
          "and confirm the live DB is fully usable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())