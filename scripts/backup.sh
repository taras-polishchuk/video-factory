#!/usr/bin/env bash
# backup.sh: DB snapshot + artifact storage + Caddy certs -> restic -> S3,
# with a sha256 manifest stored inside the snapshot.
#
# Usage:
#   ./scripts/backup.sh               # real run (prints the snapshot id on stdout)
#   ./scripts/backup.sh --dry-run     # show the plan, touch nothing
#
# Sources (two modes):
#   container (production, default when VF_COMPOSE_FILE is set):
#     - DB: online, transactionally consistent SQLCipher backup made INSIDE
#       the api container (scripts/db_snapshot.py), still encrypted with
#       VIDEO_FACTORY_DB_KEY. Never a raw copy of the live WAL database.
#     - storage: <vf_data volume>/storage, read from the host.
#     - Caddy certs/ACME account: the caddy_data volume (A2 F-28), so a
#       rebuilt host does not have to re-issue certificates.
#   file (dev / tests, when VF_COMPOSE_FILE is unset):
#     - DB: copy of $VIDEO_FACTORY_DB (stop the app first: not crash-safe)
#     - storage: $VIDEO_FACTORY_STORAGE, Caddy data: $CADDY_DATA_DIR (optional)
#   Both modes also include the Caddyfile(s) and infra/opentofu config.
#
# Manifest (A2 F-22): before restic runs, sha256 of every file that goes
# into the snapshot is written to
#   $VF_BACKUP_STAGING/snapshot/vf-backup-manifest/sha256sum.txt
# with paths relative to / (restic keeps absolute paths). That file is
# part of the snapshot, so
#   ./scripts/restore.sh --snapshot <id> --to <dir> --verify-checksum snapshot
# verifies the restored tree with no out-of-band file. A copy is also
# kept at $VF_BACKUP_STAGING/last-sha256sum.txt.
#
# Environment (required unless --dry-run):
#   RESTIC_REPOSITORY, RESTIC_PASSWORD   restic repo + password
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY   (only for s3: repositories)
# Optional:
#   RESTIC_RETENTION_DAYS   default 30
#   VF_BACKUP_STAGING       default /var/backups/video-factory
#   VF_COMPOSE_FILE         compose file -> container mode
#   VF_COMPOSE_PROJECT      default video-factory
#   VF_COMPOSE_ENV_FILE     default .env.production next to the compose file
#   VIDEO_FACTORY_DB, VIDEO_FACTORY_STORAGE, CADDY_DATA_DIR   (file mode /
#                           overrides; container mode discovers volumes)
#
# Idempotency: every run makes a new snapshot (restic deduplicates data).

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--dry-run]
Backs up the Video Factory DB, artifact storage and Caddy data to a restic
repo, with a sha256 manifest inside the snapshot.

Required env (ignored in --dry-run):
  RESTIC_REPOSITORY, RESTIC_PASSWORD (+ AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY for s3:)
Optional:
  RESTIC_RETENTION_DAYS (default 30), VF_BACKUP_STAGING, VF_COMPOSE_FILE,
  VF_COMPOSE_PROJECT, VF_COMPOSE_ENV_FILE, VIDEO_FACTORY_DB,
  VIDEO_FACTORY_STORAGE, CADDY_DATA_DIR
USAGE
      exit 0
      ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# ─── Helper: redact secrets from accidental log lines ───────────
redact() {
  sed -E \
    -e 's#(sk_live|sk_test|rk_live|rk_test)_[A-Za-z0-9]{8,}#\1_<redacted>#g' \
    -e 's#whsec_[A-Za-z0-9]{8,}#whsec_<redacted>#g' \
    -e 's#AKIA[0-9A-Z]{16}#AKIA<redacted>#g' \
    -e 's#(Bearer[[:space:]]+)[A-Za-z0-9._~+/=-]{12,}#\1<redacted>#g' \
    -e 's#((CLOUDFLARE_API_TOKEN|HETZNER_TOKEN|RESTIC_PASSWORD|AWS_SECRET_ACCESS_KEY|VIDEO_FACTORY_[A-Z_]*KEY|VIDEO_FACTORY_SESSION_SECRET)=)[^[:space:]]+#\1<redacted>#g'
}

log() {
  echo "[$(date -u +%FT%TZ)] $*" | redact >&2
}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RETENTION_DAYS="${RESTIC_RETENTION_DAYS:-30}"
STAGING_ROOT="${VF_BACKUP_STAGING:-/var/backups/video-factory}"
MODE=file
if [ -n "${VF_COMPOSE_FILE:-}" ]; then MODE=container; fi
PROJECT="${VF_COMPOSE_PROJECT:-video-factory}"

case "$RETENTION_DAYS" in
  ''|*[!0-9]*) log "ERROR: RESTIC_RETENTION_DAYS must be a positive integer"; exit 2 ;;
esac

# ─── Dry run: describe, touch nothing ──────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
  cat <<EOF
DRY RUN PLAN (mode: $MODE)
  1. DB snapshot -> $STAGING_ROOT/snapshot/db/video_factory.db
     $( [ "$MODE" = container ] && echo "(docker compose -p $PROJECT exec -T api python /app/scripts/db_snapshot.py --stdout)" || echo "(copy of ${VIDEO_FACTORY_DB:-/data/video_factory.db})" )
  2. sha256 manifest -> $STAGING_ROOT/snapshot/vf-backup-manifest/sha256sum.txt
     over: DB snapshot, storage (${VIDEO_FACTORY_STORAGE:-<vf_data volume>/storage}),
           Caddy data (${CADDY_DATA_DIR:-<caddy_data volume>}), Caddyfile(s), infra/opentofu
  3. restic -r "${RESTIC_REPOSITORY:-<RESTIC_REPOSITORY>}" backup \\
       --tag video-factory --tag backup-script --tag manifest-v1 <paths above>
  4. restic forget --group-by host,tag,paths \\
       --keep-daily $RETENTION_DAYS --keep-within "${RETENTION_DAYS}d" --prune
  5. restic check
EOF
  log "dry-run complete; no snapshots taken"
  exit 0
fi

# ─── Pre-flight ─────────────────────────────────────────────────
if ! command -v restic >/dev/null 2>&1; then
  log "ERROR: restic not on PATH; install restic (scripts/remote-bootstrap.sh does) or run with --dry-run"
  exit 1
fi
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be set}"
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD must be set}"
export RESTIC_PASSWORD
case "$RESTIC_REPOSITORY" in
  s3:*)
    : "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID must be set for an s3: repository}"
    : "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY must be set for an s3: repository}"
    ;;
esac

volume_path() {
  docker volume inspect -f '{{.Mountpoint}}' "$1" 2>/dev/null || true
}

STORAGE_DIR=""
CADDY_DIR="${CADDY_DATA_DIR:-}"
if [ "$MODE" = container ]; then
  ENV_FILE="${VF_COMPOSE_ENV_FILE:-$(dirname "$VF_COMPOSE_FILE")/.env.production}"
  COMPOSE=(docker compose -p "$PROJECT" -f "$VF_COMPOSE_FILE" --env-file "$ENV_FILE")
  DATA_VOL="$(volume_path "${PROJECT}_vf_data")"
  STORAGE_DIR="${VIDEO_FACTORY_STORAGE:-${DATA_VOL:+$DATA_VOL/storage}}"
  if [ -z "$CADDY_DIR" ]; then CADDY_DIR="$(volume_path "${PROJECT}_caddy_data")"; fi
else
  : "${VIDEO_FACTORY_DB:?VIDEO_FACTORY_DB must be set (file mode)}"
  : "${VIDEO_FACTORY_STORAGE:?VIDEO_FACTORY_STORAGE must be set (file mode)}"
  STORAGE_DIR="$VIDEO_FACTORY_STORAGE"
  if [ ! -f "$VIDEO_FACTORY_DB" ]; then
    log "ERROR: VIDEO_FACTORY_DB=$VIDEO_FACTORY_DB does not exist"
    exit 1
  fi
fi

# Ensure the restic repo is initialised (`restic cat config` fails on a
# missing repo).
if ! restic -r "$RESTIC_REPOSITORY" cat config >/dev/null 2>&1; then
  log "init: bootstrapping restic repo"
  restic -r "$RESTIC_REPOSITORY" init >/dev/null
fi

# ─── 1) Stage the DB snapshot ──────────────────────────────────
umask 077
mkdir -p "$STAGING_ROOT"
STAGING_ROOT="$(cd "$STAGING_ROOT" && pwd -P)"
# One backup at a time.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$STAGING_ROOT/.lock"
  if ! flock -n 9; then
    log "ERROR: another backup.sh run holds $STAGING_ROOT/.lock"
    exit 1
  fi
fi
# Fixed path on purpose: `restic forget --group-by host,tag,paths` groups
# snapshots by their path list, so a per-run directory name would put
# every snapshot in its own group and retention would never prune.
RUN_DIR="$STAGING_ROOT/snapshot"
rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR/db" "$RUN_DIR/vf-backup-manifest"
cleanup() { rm -rf "$RUN_DIR"; }
trap cleanup EXIT

DB_SNAPSHOT="$RUN_DIR/db/video_factory.db"
if [ "$MODE" = container ]; then
  log "db: online snapshot inside the api container"
  "${COMPOSE[@]}" exec -T api python /app/scripts/db_snapshot.py --stdout > "$DB_SNAPSHOT"
else
  log "db: file copy of the DB (file mode; stop the app for a crash-safe copy)"
  cp -p "$VIDEO_FACTORY_DB" "$DB_SNAPSHOT"
fi
if [ ! -s "$DB_SNAPSHOT" ]; then
  log "ERROR: DB snapshot is empty"
  exit 1
fi

# ─── 2) Collect the paths and write the manifest ───────────────
abspath() { (cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")"); }

BACKUP_PATHS=("$RUN_DIR")
add_path() {
  local p="$1" label="$2"
  if [ -n "$p" ] && [ -e "$p" ]; then
    BACKUP_PATHS+=("$(abspath "$p")")
  else
    log "WARNING: $label not found (${p:-unset}); not in this snapshot"
  fi
}
add_path "$STORAGE_DIR" "storage dir"
add_path "$CADDY_DIR" "caddy data dir (TLS certs / ACME account)"
add_path "$ROOT/Caddyfile" "Caddyfile template"
add_path "${VIDEO_FACTORY_CADDYFILE:-/etc/video-factory/Caddyfile}" "rendered Caddyfile"
add_path "$ROOT/infra/opentofu" "infra/opentofu"

MANIFEST="$RUN_DIR/vf-backup-manifest/sha256sum.txt"
rel=()
for p in "${BACKUP_PATHS[@]}"; do rel+=("${p#/}"); done
(
  cd /
  find "${rel[@]}" -type f ! -path '*/vf-backup-manifest/*' ! -path '*/.terraform/*' -print0 \
    | sort -z | xargs -0 -r sha256sum
) > "$MANIFEST"
{
  echo "created_utc=$(date -u +%FT%TZ)"
  echo "mode=$MODE"
  echo "files=$(wc -l < "$MANIFEST" | tr -d ' ')"
  printf 'path=%s\n' "${BACKUP_PATHS[@]}"
} > "$RUN_DIR/vf-backup-manifest/meta.txt"
log "manifest: $(wc -l < "$MANIFEST" | tr -d ' ') files hashed"

# ─── 3) restic backup ──────────────────────────────────────────
log "backup: starting restic backup"
SNAPSHOT_OUTPUT="$(restic -r "$RESTIC_REPOSITORY" backup \
  --tag video-factory \
  --tag backup-script \
  --tag manifest-v1 \
  --exclude '**/.terraform' \
  "${BACKUP_PATHS[@]}" 2>&1 | redact)"
SNAPSHOT_ID="$(echo "$SNAPSHOT_OUTPUT" | sed -n 's/^snapshot \([a-f0-9]\{1,\}\) saved$/\1/p' | head -n 1)"
if [ -z "$SNAPSHOT_ID" ]; then
  log "ERROR: restic backup did not produce a snapshot id. Output:"
  echo "$SNAPSHOT_OUTPUT" >&2
  exit 1
fi
log "backup: new snapshot id = $SNAPSHOT_ID"
cp "$MANIFEST" "$STAGING_ROOT/last-sha256sum.txt"
echo "$SNAPSHOT_ID" > "$STAGING_ROOT/last-snapshot-id"

# ─── 4) retention + 5) integrity ───────────────────────────────
log "forget: applying ${RETENTION_DAYS}-day retention"
restic -r "$RESTIC_REPOSITORY" forget \
  --group-by host,tag,paths \
  --keep-daily "$RETENTION_DAYS" \
  --keep-within "${RETENTION_DAYS}d" \
  --prune >/dev/null

log "check: verifying repo integrity"
restic -r "$RESTIC_REPOSITORY" check >/dev/null

echo "$SNAPSHOT_ID"
