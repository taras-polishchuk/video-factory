"""One-time helper: write missing artifact HMAC sidecars.

SECURITY S1a made every download route verify the Wave 5 sidecar
and fail closed (404).  Blobs written before every write path
tagged them have no sidecar:

  * assets uploaded through the legacy
    ``POST /api/v1/companies/{cid}/assets`` route,
  * final MP4s from the assembly stage.

Run once after upgrading a deployment that already holds data:

    VIDEO_FACTORY_DB=... VIDEO_FACTORY_STORAGE=... \\
        python scripts/backfill_artifact_sidecars.py          # count only
    VIDEO_FACTORY_DB=... VIDEO_FACTORY_STORAGE=... \\
        python scripts/backfill_artifact_sidecars.py --apply  # write

Use the same ``VIDEO_FACTORY_DB_KEY`` / ``VIDEO_FACTORY_SECRET_KEY``
as the API, or the sidecars will not verify.  Existing sidecars
that do not verify are reported, never overwritten.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from control_plane import artifact_hmac
    from core.repo import Repo

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the missing sidecars (default: count only)")
    args = ap.parse_args(argv)
    db = os.environ.get("VIDEO_FACTORY_DB", "").strip()
    storage = os.environ.get("VIDEO_FACTORY_STORAGE", "").strip()
    if not db or not storage:
        print("VIDEO_FACTORY_DB and VIDEO_FACTORY_STORAGE must be set",
              file=sys.stderr)
        return 2
    warn = artifact_hmac.warn_if_dev_key_in_use()
    if warn and args.apply:
        print(f"refusing to --apply with the dev key: {warn}",
              file=sys.stderr)
        return 2
    stats = artifact_hmac.backfill_missing_sidecars(
        Repo(db), storage_root=storage, apply=args.apply,
    )
    print(json.dumps({"apply": args.apply, **stats}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
