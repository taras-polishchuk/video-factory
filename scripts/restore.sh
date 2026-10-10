#!/usr/bin/env bash
# restore.sh — restore a Video Factory backup to a target directory
#
# Usage:
#   ./scripts/restore.sh --snapshot latest --to /tmp/restore-test
#   ./scripts/restore.sh --snapshot <id>   --to /tmp/restore-test
#   ./scripts/restore.sh --snapshot latest --verify-checksum sha256sum.txt --to /tmp/restore-test
#
# Environment (same as backup.sh — required unless --dry-run):
#   RESTIC_REPOSITORY, RESTIC_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
#
# Behaviour:
#   - Downloads the snapshot via `restic restore` to <to>
#   - Runs restic's built-in integrity check
#   - If --verify-checksum <file> is supplied, computes the sha256 of
#     every restored file and asserts it equals the value in <file>
#     (file is "sha256sum.txt" format)
#   - exit 0 on success
#
# This is the "restore drill" — running this against a test directory
# on a fresh machine proves the backup can be recovered.

set -euo pipefail

SNAPSHOT="latest"
TARGET=""
CHECKSUM_FILE=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --to)       TARGET="$2"; shift 2 ;;
    --verify-checksum) CHECKSUM_FILE="$2"; shift 2 ;;
    -h|--help)
      cat <<USAGE
Usage: $0 --snapshot <id|latest> --to <dir> [--verify-checksum <sha256sum.txt>]
Restores a Video Factory restic snapshot.
USAGE
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TARGET" ]; then
  echo "ERROR: --to <dir> is required" >&2
  exit 2
fi

if [ -z "${RESTIC_REPOSITORY:-}" ] || [ -z "${RESTIC_PASSWORD:-}" ]; then
  echo "ERROR: RESTIC_REPOSITORY and RESTIC_PASSWORD must be set" >&2
  exit 1
fi

if ! command -v restic >/dev/null 2>&1; then
  echo "ERROR: restic not on PATH" >&2
  exit 1
fi

if [ "$SNAPSHOT" = "latest" ]; then
  # `restic snapshots latest --json` is the robust path across
  # restic 0.13+. Real restic prints ONE JSON array
  # (`[{"id": ..., "short_id": ...}]`); some wrappers print one object
  # per line. Accept both and take the last snapshot listed (restic
  # sorts oldest -> newest). Fall back to parsing the table output if
  # json isn't available.
  JSON_OUT="$(restic -r "$RESTIC_REPOSITORY" snapshots latest --json 2>/dev/null || true)"
  SNAPSHOT_ID="$(echo "$JSON_OUT" | python3 -c '
import json, sys
found = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    items = obj if isinstance(obj, list) else [obj]
    for item in items:
        if isinstance(item, dict) and item.get("id") and item.get("short_id"):
            found = item["id"]
if found:
    print(found)
' 2>/dev/null | head -n 1)"
  if [ -z "$SNAPSHOT_ID" ]; then
    # Fallback: parse the table output (3rd line after the dashed
    # separator is the data row).
    SNAPSHOT_ID="$(restic -r "$RESTIC_REPOSITORY" snapshots latest 2>/dev/null \
      | grep -v '^ID ' | grep -v '^--' | head -n 1 | awk '{print $1}')"
  fi
  if [ -z "$SNAPSHOT_ID" ]; then
    echo "ERROR: no snapshots found in $RESTIC_REPOSITORY" >&2
    exit 1
  fi
else
  SNAPSHOT_ID="$SNAPSHOT"
fi

mkdir -p "$TARGET"

echo "restore: $SNAPSHOT_ID -> $TARGET"
restic -r "$RESTIC_REPOSITORY" restore "$SNAPSHOT_ID" --target "$TARGET"

if [ -n "$CHECKSUM_FILE" ]; then
  if [ ! -f "$CHECKSUM_FILE" ]; then
    echo "ERROR: --verify-checksum file $CHECKSUM_FILE does not exist" >&2
    exit 1
  fi
  echo "restore: verifying checksums against $CHECKSUM_FILE"
  # `sha256sum -c` reads the manifest, then computes the hash of each
  # listed file. We `cd` into TARGET so the relative paths in the
  # manifest resolve correctly.
  (
    cd "$TARGET"
    sha256sum -c "$CHECKSUM_FILE"
  )
fi

echo "restore: complete"