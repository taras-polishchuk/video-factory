# Operations runbook — Video Factory SaaS (production)

Quick-reference for the operator. If something here disagrees with
what the API does, the API wins (and the runbook gets a bug).

## Layout on the host

```
/opt/video-factory/                  Repo checkout
/etc/video-factory/credentials/      systemd LoadCredential= files (0600)
/opt/video-factory/.env.production   Generated, 0600, .gitignore'd
/etc/systemd/system/video-factory.service
/usr/local/bin/video-factory-load-env.sh
/var/log/video-factory/              Service logs (journalctl -u video-factory)
restic repo at $RESTIC_REPOSITORY    Daily backup target
```

---

## Rotate the DB master key (VIDEO_FACTORY_DB_KEY)

`VIDEO_FACTORY_DB_KEY` is the SQLCipher page key of
`/data/video_factory.db`. Rotation is done live by the owner-only admin
endpoint (`control_plane/key_rotation.py`): it copies the DB into
`<db>.new` under the new key, verifies row counts, swaps the files and
writes `key.rotation_completed` to the audit log. There is no
`scripts/rotate_master_key.sh`; the endpoint is the tool.

```bash
# 1) Generate the new key (64 hex chars) on the host, never in chat/logs
NEW_KEY=$(openssl rand -hex 32)
OLD_KEY=$(cat /etc/video-factory/credentials/video_factory_db_key)

# 2) State before
curl -fsS https://$DOMAIN/api/v1/admin/keys/status \
  -H "Cookie: vf_session=$OWNER_SESSION_COOKIE"

# 3) Rotate (owner session; old + new key are both required)
curl -fsS -X POST https://$DOMAIN/api/v1/admin/keys/rotate \
  -H "Cookie: vf_session=$OWNER_SESSION_COOKIE" \
  -H "Content-Type: application/json" \
  -d "{\"old_key\": \"$OLD_KEY\", \"new_key\": \"$NEW_KEY\"}"

# 4) Persist the new key atomically, then restart so every process
#    and the next boot use it
umask 077
printf '%s' "$NEW_KEY" > /etc/video-factory/credentials/video_factory_db_key.new
mv -f /etc/video-factory/credentials/video_factory_db_key.new \
      /etc/video-factory/credentials/video_factory_db_key
unset NEW_KEY OLD_KEY
systemctl restart video-factory.service
```

503 from step 3 = another rotation is running. 400 = wrong old key.
Take a backup (`systemctl start video-factory-backup.service`) BEFORE
rotating: snapshots taken earlier stay encrypted with the old key, so
keep the old key in the password manager next to the snapshot dates.

`VIDEO_FACTORY_SECRET_KEY` (Fernet, encrypts provider credentials) has
NO in-place re-encryption. Replacing it makes every stored provider
credential unreadable; re-enter them in the UI after the swap.

---

## Backups

`video-factory-backup.timer` runs `scripts/backup.sh` daily. One restic
snapshot holds: a consistent online copy of the DB (SQLite backup API,
still SQLCipher-encrypted), job storage, the Caddy data volume (TLS
certs + ACME account), the rendered Caddyfile and `infra/opentofu`
state, plus `vf-backup-manifest/sha256sum.txt` (sha256 of every file
in the snapshot).

```bash
systemctl start video-factory-backup.service     # run one now
journalctl -u video-factory-backup.service -n 50
restic -r "$RESTIC_REPOSITORY" snapshots --tag video-factory
```

## Restore from backup

```bash
# 1) Restore to a scratch dir and verify restic data + the manifest
./scripts/restore.sh --snapshot latest --to /tmp/restore-test --verify-checksum snapshot

# 2) Stop the service so the live DB file is not held open
systemctl stop video-factory.service

# 3) Copy the restored files into the vf_data volume
#    (restic keeps absolute paths; the DB is under */snapshot/db/)
VOL=$(docker volume inspect -f '{{.Mountpoint}}' video-factory_vf_data)
DB=$(find /tmp/restore-test -path '*/snapshot/db/video_factory.db' | head -n 1)
mv "$VOL/video_factory.db" "$VOL/video_factory.db.bak"
cp "$DB" "$VOL/video_factory.db"
# storage: copy the restored */_data/storage tree the same way

# 4) Start and check
systemctl start video-factory.service
curl -fsS https://$DOMAIN/healthz   # 200
curl -fsS https://$DOMAIN/readyz    # 200 (DB opens with the current key)
```

The restored DB opens only with the `VIDEO_FACTORY_DB_KEY` that was live
when the snapshot was taken.

Restore drill (fresh machine or scratch dir) **monthly**:

```bash
./scripts/restore.sh --snapshot latest --to /tmp/drill --verify-checksum snapshot
```

---

## Force-logout all sessions

Sessions live in the `sessions` table. To invalidate every session
(for a particular user, or globally after a credential leak):

```bash
# Logout a single user (kills every session for that user_id)
sqlite3 /data/video_factory.db \
  "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE email = 'taras@example.com')"

# Logout everyone
sqlite3 /data/video_factory.db "DELETE FROM sessions"

# Cookie attribute: rotates the next session cookie, so a stolen
# cookie is rejected.
systemctl restart video-factory.service
```

After a credential leak, ALSO rotate VIDEO_FACTORY_SESSION_SECRET
(see rotation above) AND any provider credentials that were in scope.

---

## Change a workspace's Stripe plan

Plans are derived from the Stripe subscription via webhook. The
operator rarely edits `workspaces.plan` by hand. When you must:

```bash
sqlite3 /data/video_factory.db \
  "UPDATE workspaces
   SET plan = 'enterprise'
   WHERE id = 'ws_xxx'"
```

The Stripe webhook will overwrite this on the next invoice event. If
you want to lock the override, set `workspaces.manual_plan_lock=1`:

```bash
sqlite3 /data/video_factory.db \
  "UPDATE workspaces SET manual_plan_lock = 1 WHERE id = 'ws_xxx'"
```

---

## View the audit log

```bash
# Last 50 rows, any workspace
sqlite3 -header -column /data/video_factory.db \
  "SELECT id, event, actor, target, meta_json, created_at
   FROM audit_log ORDER BY id DESC LIMIT 50"

# Audit log for one workspace
sqlite3 -header -column /data/video_factory.db \
  "SELECT id, event, actor, target, created_at
   FROM audit_log WHERE workspace_id = 'ws_xxx' ORDER BY id DESC LIMIT 100"

# Audit log for one user
sqlite3 -header -column /data/video_factory.db \
  "SELECT id, event, target, created_at
   FROM audit_log WHERE actor = 'user_xxx' ORDER BY id DESC LIMIT 100"
```

The UI exposes the same view at `/audit` for owner + admin roles
only.

---

## Lock a user

```bash
sqlite3 /data/video_factory.db \
  "UPDATE users SET status = 'locked', locked_reason = 'manual' WHERE email = 'taras@example.com'"
```

`status='locked'` rejects login (HTTP 423) but preserves the row for
audit. To unlock:

```bash
sqlite3 /data/video_factory.db \
  "UPDATE users SET status = 'active', locked_reason = NULL WHERE email = 'taras@example.com'"
```

The UI shows a "locked" badge at `/admin/users` and refuses to issue
new sessions.

---

## Restart the stack

```bash
systemctl restart video-factory.service
```

Equivalent to `./scripts/provision-and-deploy.sh $DOMAIN --apply` but
faster (no rsync, no tofu).

---

## Re-issue a TLS cert

Certs auto-renew via DNS-01. The Caddy image is built from
`infra/caddy/Dockerfile` with the `caddy-dns/cloudflare` module (the
stock `caddy:2` image cannot do `dns cloudflare`). Caddy has no
"renew now" command; if renewal fails (Cloudflare rate limit, token
scope), fix the cause and restart Caddy, which retries at once:

```bash
cd /opt/video-factory
docker compose -f docker-compose.production.yml logs --tail 100 caddy
docker compose -f docker-compose.production.yml exec caddy caddy list-modules | grep dns.providers.cloudflare
docker compose -f docker-compose.production.yml restart caddy
```

Cert files live in the `caddy_data` volume (included in every backup).

---

## Common error shapes

| Symptom | Cause | Fix |
|---|---|---|
| 503 on every endpoint | DB locked (wrong VIDEO_FACTORY_DB_KEY) | Rotate the key or restore from backup |
| Stripe webhook 401 | STRIPE_WEBHOOK_SECRET rotated | Update credential, restart service |
| OIDC callback 400 (state) | clock skew > 10 min | `timedatectl set-ntp true` |
| /metrics empty | scrape from wrong IP | Use the api container's loopback, port 8000 |
| `restored DB is unreadable` | VIDEO_FACTORY_DB_KEY changed | Use the key that was live at snapshot time |

For anything not listed here: capture the request id from the
response header `X-Request-Id`, then `journalctl -u video-factory
--since "5 minutes ago" | grep <request-id>` to find the matching
log line.