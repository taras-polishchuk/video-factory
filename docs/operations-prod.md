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

## Rotate the master Fernet key (VIDEO_FACTORY_SECRET_KEY)

The Fernet key encrypts all provider credentials in `credentials`.
Rotation re-encrypts every row under the new key without downtime.

```bash
# 1) Generate a fresh key
NEW_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
echo "new key generated: $NEW_KEY"

# 2) Stash it in the credential directory
op read "op://video-factory/video_factory_secret_key" \
  > /etc/video-factory/credentials/video_factory_secret_key.new

# 3) Run the rotation
systemctl stop video-factory.service   # one-time downtime is acceptable
mv /etc/video-factory/credentials/video_factory_secret_key.new \
   /etc/video-factory/credentials/video_factory_secret_key
chmod 0600 /etc/video-factory/credentials/video_factory_secret_key
systemctl start video-factory.service

# 4) Confirm via audit
sqlite3 /data/video_factory.db \
  "SELECT event, actor, meta_json FROM audit_log WHERE event LIKE 'key.%' ORDER BY id DESC LIMIT 5"
# Expect: key.rotation_completed with both key fingerprints.
```

For a live (no-downtime) rotation, run the admin endpoint instead.
S1b: the endpoint needs an owner/admin session, its CSRF token and
the caller's CURRENT PASSWORD in the body (re-authentication; 5
attempts per 15 min per user). The body goes in via stdin from
exported variables, so neither key nor password lands in `ps`,
shell history or a proxy access log:

```bash
CSRF=$(curl -fsS https://$DOMAIN/auth/csrf \
  -H "Cookie: vf_session=$ADMIN_SESSION_COOKIE" | jq -r .csrf_token)
read -rs -p "current password: " VF_PW; echo
export OLD_KEY NEW_KEY VF_PW
jq -n '{old_key: env.OLD_KEY, new_key: env.NEW_KEY, current_password: env.VF_PW}' \
  | curl -fsS -X POST https://$DOMAIN/api/v1/admin/keys/rotate \
      -H "Cookie: vf_session=$ADMIN_SESSION_COOKIE" \
      -H "X-CSRF-Token: $CSRF" \
      -H "Content-Type: application/json" --data-binary @-
unset VF_PW OLD_KEY NEW_KEY
```

The endpoint sets `key.rotation_in_progress=1`, blocks new credential
writes during the rotation, and writes `key.rotation_completed` on
success. Audit log, app logs and error bodies carry only the SHA-256
fingerprints of the keys, never the keys. 403 = missing CSRF, wrong
password or no owner/admin role; 429 = re-auth budget used up.

---

## Restore from backup

```bash
# 1) Find the most recent snapshot
restic -r "$RESTIC_REPOSITORY" snapshots latest

# 2) Stop the service so the restored DB file is not held open
systemctl stop video-factory.service

# 3) Restore to a scratch directory first
./scripts/restore.sh --snapshot <id> --to /tmp/restore-test

# 4) Verify the checksum
cd /tmp/restore-test
sha256sum -c sha256sum.txt    # if available

# 5) Move the restored files into place
mv /data/video_factory.db /data/video_factory.db.bak
mv /data/storage /data/storage.bak
cp /tmp/restore-test/video_factory.db /data/
cp -a /tmp/restore-test/storage /data/

# 6) Start the service
systemctl start video-factory.service
curl -fsS https://$DOMAIN/health   # 200
```

Run a restore drill (against a fresh machine) **monthly**:

```bash
./scripts/restore.sh --snapshot latest --to /tmp/drill --verify-checksum sha256sum.txt
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
(see "Session secret rotation" below) AND any provider credentials
that were in scope.

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

Certs auto-renew via DNS-01 (Caddy). If renewal fails (cloudflare
rate-limit, network blip), force it:

```bash
docker compose -f /opt/video-factory/docker-compose.production.yml \
  exec caddy caddy renew --force
```

Inspect current certs:

```bash
docker compose exec caddy caddy list-certs
```

---

## Common error shapes

| Symptom | Cause | Fix |
|---|---|---|
| 503 on every endpoint | DB locked (wrong VIDEO_FACTORY_DB_KEY) | Rotate the key or restore from backup |
| Stripe webhook 401 | STRIPE_WEBHOOK_SECRET rotated | Update credential, restart service |
| OIDC callback 400 (state) | clock skew > 10 min | `timedatectl set-ntp true` |
| /metrics empty | scrape from wrong IP | Use the api container's loopback, port 8000 |
| `restored DB is unreadable` | VIDEO_FACTORY_DB_KEY changed | Match the key in `/etc/video-factory/credentials/video_factory_db_key` |

For anything not listed here: capture the request id from the
response header `X-Request-Id`, then `journalctl -u video-factory
--since "5 minutes ago" | grep <request-id>` to find the matching
log line.
---

## Session secret rotation (VIDEO_FACTORY_SESSION_SECRET)

Sessions are opaque ids in the `sessions` table; the secret only
signs CSRF tokens (`HMAC(secret, session_id)`) and the SvelteKit
server's internal-call token. `VIDEO_FACTORY_SESSION_SECRET_PREVIOUS`
(one value or a comma-separated list) is accepted for VERIFICATION
ONLY, so a planned rotation logs nobody out:

```bash
# 1) Generate the new secret on the host
NEW=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

# 2) Old value -> VIDEO_FACTORY_SESSION_SECRET_PREVIOUS,
#    new value -> VIDEO_FACTORY_SESSION_SECRET, for BOTH the api and
#    the web container (the web server derives CSRF + internal tokens
#    with the same secret). Restart api + web.

# 3) After 7 days (session TTL; open tabs keep their CSRF token in
#    memory) remove VIDEO_FACTORY_SESSION_SECRET_PREVIOUS and restart.
```

New tokens are always derived from the current secret. After a LEAK do
not use the overlap: set only the new secret (no `_PREVIOUS`) and run
"Logout everyone" above, so every old token and session dies at once.

---

## Pre-session auth hardening (S1b)

Settings that change how `/auth/signup`, `/auth/login`, `/auth/forgot`,
`/auth/forgot/consume` and `/auth/verify` behave:

| Env var | Default | Meaning |
|---|---|---|
| `VIDEO_FACTORY_ENV` | unset (dev) | `production` / `staging`: Secure cookies, Origin required |
| `VIDEO_FACTORY_PUBLIC_BASE_URL` | unset | Its origin is allow-listed for the Origin check |
| `VIDEO_FACTORY_CORS_ORIGINS` | unset | Extra allowed origins; also the ONLY way to enable CORS (`*` is ignored) |
| `VIDEO_FACTORY_REQUIRE_ORIGIN` | from `VIDEO_FACTORY_ENV` | `1`/`0` forces strict / lenient mode |
| `VIDEO_FACTORY_TRUSTED_PROXIES` | `127.0.0.0/8,::1/128,172.16.0.0/12` | Peers whose `X-Forwarded-For` / `X-Forwarded-Proto` are believed |
| `VF_SIGNUP_IP_LIMIT` / `_WINDOW_SEC` | 10 / 3600 | Signups per client IP |
| `VF_FORGOT_IP_LIMIT` / `_WINDOW_SEC` | 5 / 900 | Reset requests per IP |
| `VF_FORGOT_EMAIL_LIMIT` / `_WINDOW_SEC` | 3 / 3600 | Reset requests per address (stored as a hash) |
| `VF_RESET_CONSUME_IP_LIMIT` / `_WINDOW_SEC` | 10 / 900 | Reset-token submissions per IP |
| `VF_VERIFY_IP_LIMIT` / `_WINDOW_SEC` | 10 / 900 | Verify-token submissions per IP |
| `VF_KEYS_REAUTH_LIMIT` / `_WINDOW_SEC` | 5 / 900 | Password re-auth on key rotation, per user |

Origin rule (auth/origin.py): a valid `X-VF-Internal-Token` (sent only
by the SvelteKit server, derived from the session secret) passes; else
`Origin` (or `Referer`) must be the request's own host, the public base
URL or a CORS origin; `Origin: null` never passes. With neither header
the call is allowed in dev and refused (403 `origin_missing`) in
production / staging. A curl smoke test against production must send
`-H "Origin: https://$DOMAIN"`.

Client IP (auth/client_ip.py): `X-Forwarded-For` is read only when the
socket peer is a trusted proxy, right to left, first untrusted hop
wins. The SvelteKit server appends `event.getClientAddress()` to the
chain it got from Caddy, so each browser gets its own bucket. A 429
carries `Retry-After`; counters live in `rate_limit_hits` (pruned
after 7 days). Reset one client:

```bash
sqlite3 /data/video_factory.db "DELETE FROM rate_limit_hits WHERE key = '203.0.113.7'"
```

HeyGen webhook replays: every accepted signed body is recorded in
`webhook_receipts` (SHA-256 of the body, kept
`HEYGEN_WEBHOOK_DEDUP_DAYS`, default 30). A byte-identical resend gets
`200 {"duplicate": true}` and changes nothing.
