#!/usr/bin/env bash
# backup.sh — encrypted SQLite DB + artifact sidecars → restic → S3
#
# Usage:
#   ./scripts/backup.sh                     # real run
#   ./scripts/backup.sh --dry-run           # show what would happen, do nothing
#   RESTIC_REPOSITORY=s3:... ./scripts/backup.sh
#
# Environment (all required when NOT --dry-run):
#   VIDEO_FACTORY_DB        — path to the SQLCipher DB file (e.g. /data/video_factory.db)
#   VIDEO_FACTORY_STORAGE   — path to the artifact storage root (e.g. /data/storage)
#   RESTIC_REPOSITORY       — restic repo (e.g. s3:s3.amazonaws.com/video-factory-backup/prod)
#   RESTIC_PASSWORD         — restic repo password
#   AWS_ACCESS_KEY_ID       — S3 credentials
#   AWS_SECRET_ACCESS_KEY   — S3 credentials
#   RESTIC_RETENTION_DAYS   — default 30
#
# Behaviour:
#   - snapshot VIDEO_FACTORY_DB + VIDEO_FACTORY_STORAGE + Caddyfile + Tofu state
#   - retention: --keep-within 30d --keep-daily 30 (configurable via RESTIC_RETENTION_DAYS)
#   - prints the new snapshot id on stdout (so cron can capture it)
#   - exit 0 on success, non-zero on any failure (fail-fast)
#
# Idempotency:
#   Running twice in a row produces two snapshots with distinct ids.
#   That's expected — restic deduplicates data and pricing is per-data,
#   not per-snapshot. The cron schedule is daily.

set -eo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--dry-run]
Backs up the Video Factory DB + artifacts to a restic repo.

Required env (ignored in --dry-run):
  VIDEO_FACTORY_DB, VIDEO_FACTORY_STORAGE, RESTIC_REPOSITORY, RESTIC_PASSWORD,
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
Optional:
  RESTIC_RETENTION_DAYS (default 30)
USAGE
      exit 0
      ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# ─── Helper: redact secrets from accidental log lines ───────────
redact() {
  # Replace anything that looks like a token / key with <redacted>.
  sed -E \
    -e 's#(sk_live|sk_test)_[A-Za-z0-9]{8,}[A-Za-z0-9]*#\1_<redacted>#g' \
    -e 's#(Bearer[[:space:]]+)[A-Za-z0-9._\-]{12,}#\1<redacted>#g' \
    -e 's#(CLOUDFLARE_API_TOKEN=)[A-Za-z0-9._\-]{12,}#\1<redacted>#g' \
    -e 's#(HETZNER_TOKEN=)[A-Za-z0-9._\-]{12,}#\1<redacted>#g'
}

log() {
  echo "[$(date -u +%FT%TZ)] $*" | redact >&2
}

# ─── Pre-flight ─────────────────────────────────────────────────
if [ "$DRY_RUN" -eq 0 ]; then
  if ! command -v restic >/dev/null 2>&1; then
    log "ERROR: restic not on PATH; install restic (e.g. 'apt install restic') or run with --dry-run"
    exit 1
  fi

  : "${VIDEO_FACTORY_DB:?VIDEO_FACTORY_DB must be set}"
  : "${VIDEO_FACTORY_STORAGE:?VIDEO_FACTORY_STORAGE must be set}"
  : "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be set}"
  : "${RESTIC_PASSWORD:?RESTIC_PASSWORD must be set}"
  : "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID must be set}"
  : "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY must be set}"

  if [ ! -f "$VIDEO_FACTORY_DB" ]; then
    log "ERROR: VIDEO_FACTORY_DB=$VIDEO_FACTORY_DB does not exist"
    exit 1
  fi

  # Ensure the restic repo is initialised. `restic snapshots` exits
  # non-zero when the repo does not exist yet.
  if ! restic snapshots >/dev/null 2>&1; then
    log "init: bootstrapping restic repo at $RESTIC_REPOSITORY"
    restic init >/dev/null
  fi
fi

# ─── What we will (or do) snapshot ─────────────────────────────
BACKUP_PATHS=(
  "$VIDEO_FACTORY_DB"
  "$VIDEO_FACTORY_STORAGE"
  "./Caddyfile"
  "./infra/opentofu"
)
RETENTION_DAYS="${RESTIC_RETENTION_DAYS:-30}"

# In --dry-run we ignore unset vars (the script may be run on a host
# that doesn't have the real env). We still need *something* in
# VIDEO_FACTORY_DB / VIDEO_FACTORY_STORAGE to be informative.
if [ "$DRY_RUN" -eq 1 ]; then
  BACKUP_PATHS=(
    "${VIDEO_FACTORY_DB:-/data/video_factory.db}"
    "${VIDEO_FACTORY_STORAGE:-/data/storage}"
    "./Caddyfile"
    "./infra/opentofu"
  )
fi

log "would backup: ${BACKUP_PATHS[*]}"

if [ "$DRY_RUN" -eq 1 ]; then
  cat <<EOF
DRY RUN PLAN
  restic -r "$RESTIC_REPOSITORY" backup \\
    --tag video-factory \\
    --tag backup-script \\
    ${BACKUP_PATHS[*]}
  restic forget \\
    --group-by host,tag,paths \\
    --keep-daily $RETENTION_DAYS \\
    --keep-within "${RETENTION_DAYS}d" \\
    --prune
  restic check
EOF
  log "dry-run complete; no snapshots taken"
  exit 0
fi

# ─── Real run ──────────────────────────────────────────────────
log "backup: starting restic backup"
SNAPSHOT_OUTPUT="$(restic -r "$RESTIC_REPOSITORY" backup \
  --tag video-factory \
  --tag backup-script \
  "${BACKUP_PATHS[@]}" 2>&1 | redact)"
SNAPSHOT_ID="$(echo "$SNAPSHOT_OUTPUT" | sed -n 's/^snapshot \([a-f0-9]\+\) saved$/\1/p' | head -n 1)"
if [ -z "$SNAPSHOT_ID" ]; then
  log "ERROR: restic backup did not produce a snapshot id. Output:"
  echo "$SNAPSHOT_OUTPUT" | redact >&2
  exit 1
fi
log "backup: new snapshot id = $SNAPSHOT_ID"

log "forget: applying ${RETENTION_DAYS}-day retention"
restic -r "$RESTIC_REPOSITORY" forget \
  --group-by host,tag,paths \
  --keep-daily "$RETENTION_DAYS" \
  --keep-within "${RETENTION_DAYS}d" \
  --prune >/dev/null

log "check: verifying repo integrity"
restic -r "$RESTIC_REPOSITORY" check >/dev/null

echo "$SNAPSHOT_ID"