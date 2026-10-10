#!/usr/bin/env bash
# restore.sh: restore a Video Factory backup to a target directory
#
# Usage:
#   ./scripts/restore.sh --snapshot latest --to /tmp/restore-test
#   ./scripts/restore.sh --snapshot <id>   --to /tmp/restore-test --verify-checksum snapshot
#   ./scripts/restore.sh --snapshot latest --to /tmp/restore-test --verify-checksum /path/sha256sum.txt
#
# Environment: RESTIC_REPOSITORY, RESTIC_PASSWORD (+ AWS_ACCESS_KEY_ID,
# AWS_SECRET_ACCESS_KEY for s3: repositories).
#
# Behaviour:
#   - `restic restore <id> --target <dir> --verify` (restic re-reads every
#     restored file and compares it with the snapshot).
#   - --verify-checksum snapshot: use the manifest that scripts/backup.sh
#     stored INSIDE the snapshot (*/vf-backup-manifest/sha256sum.txt,
#     A2 F-22) and check every listed file with `sha256sum -c --strict`.
#   - --verify-checksum <file>: same check against an out-of-band manifest
#     (paths relative to the restore target).
#   - exit 0 only if every check passes.
#
# Restored layout: restic keeps absolute paths, so /var/backups/...,
# /var/lib/docker/volumes/... land under <dir>/var/... . The DB is at
# <dir>/<staging>/snapshot/db/video_factory.db (still SQLCipher-encrypted
# with the VIDEO_FACTORY_DB_KEY that was live at backup time).
#
# This is the "restore drill": run it monthly against a scratch dir.

set -euo pipefail

SNAPSHOT="latest"
TARGET=""
CHECKSUM_FILE=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --snapshot) SNAPSHOT="${2:?--snapshot needs a value}"; shift 2 ;;
    --to)       TARGET="${2:?--to needs a value}"; shift 2 ;;
    --verify-checksum) CHECKSUM_FILE="${2:?--verify-checksum needs snapshot or a file}"; shift 2 ;;
    -h|--help)
      cat <<USAGE
Usage: $0 --snapshot <id|latest> --to <dir> [--verify-checksum <snapshot|sha256sum.txt>]
Restores a Video Factory restic snapshot and verifies it.
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
export RESTIC_PASSWORD

if ! command -v restic >/dev/null 2>&1; then
  echo "ERROR: restic not on PATH" >&2
  exit 1
fi

# Resolve an out-of-band manifest BEFORE we cd anywhere.
if [ -n "$CHECKSUM_FILE" ] && [ "$CHECKSUM_FILE" != "snapshot" ]; then
  if [ ! -f "$CHECKSUM_FILE" ]; then
    echo "ERROR: --verify-checksum file $CHECKSUM_FILE does not exist" >&2
    exit 1
  fi
  CHECKSUM_FILE="$(cd "$(dirname "$CHECKSUM_FILE")" && pwd -P)/$(basename "$CHECKSUM_FILE")"
fi

if [ "$SNAPSHOT" = "latest" ]; then
  JSON_OUT="$(restic -r "$RESTIC_REPOSITORY" snapshots latest --json 2>/dev/null || true)"
  SNAPSHOT_ID="$(printf '%s\n' "$JSON_OUT" | python3 -c '
import json, sys
text = sys.stdin.read().strip()
items = []
try:
    data = json.loads(text)
    items = data if isinstance(data, list) else [data]
except Exception:
    for line in text.splitlines():
        try:
            items.append(json.loads(line))
        except Exception:
            pass
for obj in items:
    if isinstance(obj, dict) and obj.get("id"):
        print(obj["id"])
        break
' 2>/dev/null | head -n 1)"
  if [ -z "$SNAPSHOT_ID" ]; then
    SNAPSHOT_ID="$(restic -r "$RESTIC_REPOSITORY" snapshots latest 2>/dev/null \
      | grep -v '^ID ' | grep -v '^--' | head -n 1 | awk '{print $1}')"
  fi
  if [ -z "$SNAPSHOT_ID" ]; then
    echo "ERROR: no snapshots found in the repository" >&2
    exit 1
  fi
else
  SNAPSHOT_ID="$SNAPSHOT"
fi
case "$SNAPSHOT_ID" in
  *[!0-9a-f]*|'') echo "ERROR: invalid snapshot id '$SNAPSHOT_ID'" >&2; exit 2 ;;
esac

mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd -P)"

echo "restore: $SNAPSHOT_ID -> $TARGET"
restic -r "$RESTIC_REPOSITORY" restore "$SNAPSHOT_ID" --target "$TARGET" --verify

if [ -n "$CHECKSUM_FILE" ]; then
  if [ "$CHECKSUM_FILE" = "snapshot" ]; then
    mapfile -t found < <(find "$TARGET" -path '*/vf-backup-manifest/sha256sum.txt' -type f)
    if [ "${#found[@]}" -ne 1 ]; then
      echo "ERROR: expected exactly one */vf-backup-manifest/sha256sum.txt in the restore, found ${#found[@]}" >&2
      exit 1
    fi
    CHECKSUM_FILE="${found[0]}"
  fi
  if [ ! -s "$CHECKSUM_FILE" ]; then
    echo "ERROR: manifest $CHECKSUM_FILE is empty" >&2
    exit 1
  fi
  echo "restore: verifying $(wc -l < "$CHECKSUM_FILE" | tr -d ' ') checksums from $CHECKSUM_FILE"
  # Manifest paths are relative to / at backup time = relative to TARGET now.
  (
    cd "$TARGET"
    sha256sum -c --strict --quiet "$CHECKSUM_FILE"
  )
  echo "restore: checksums OK"
fi

echo "restore: complete"
