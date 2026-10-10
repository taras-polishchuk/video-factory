# Secret distribution — Video Factory production deploy
#
# Why systemd `LoadCredential=`?
# ------------------------------
# Conventional `.env` files in the repo are a known leak vector:
#   - rsync across hosts with `dotenv` artefacts
#   - accidentally committed to git
#   - readable by every process running in the container
#
# systemd's `LoadCredential=<name>:<path>` (≥ v250) mounts each
# credential file into the service as `$CREDENTIALS_DIRECTORY/<name>`
# without ever copying it to the service's working directory.
# docker compose's `--env-file` then reads the rendered file before
# the containers start.
#
# Layout on the production host
# -----------------------------
# /etc/video-factory/credentials/
#   cloudflare_api_token         mode 0600, owned by root:root
#   hetzner_token                mode 0600, owned by root:root
#   stripe_secret_key            mode 0600, owned by root:root
#   stripe_webhook_secret        mode 0600, owned by root:root
#   video_factory_secret_key     mode 0600, owned by root:root
#   video_factory_db_key         mode 0600, owned by root:root
#   video_factory_session_secret mode 0600, owned by root:root
#
# /etc/systemd/system/video-factory.service    — see systemd/video-factory.service
# /usr/local/bin/video-factory-load-env.sh     — see systemd/video-factory-load-env.sh
# /opt/video-factory/.env.production           — generated, mode 0600, .gitignore'd
#
# Provisioning steps (run once per host)
# --------------------------------------
# 1. mkdir -p /etc/video-factory/credentials
# 2. for f in cloudflare_api_token hetzner_token stripe_secret_key \
#             stripe_webhook_secret video_factory_secret_key \
#             video_factory_db_key video_factory_session_secret; do
#        install -m 0600 /dev/null /etc/video-factory/credentials/$f
#      done
# 3. Populate each credential file using your secret manager:
#        sops --decrypt secrets/video-factory/$f.enc > /etc/video-factory/credentials/$f
#      OR
#        op read "op://video-factory/$f" > /etc/video-factory/credentials/$f
# 4. Install the unit file:
#        cp systemd/video-factory.service /etc/systemd/system/
#        cp systemd/video-factory-load-env.sh /usr/local/bin/
#        chmod 0755 /usr/local/bin/video-factory-load-env.sh
# 5. systemctl daemon-reload
# 6. systemctl enable --now video-factory.service
#
# Rotation
# --------
# To rotate any secret in place:
#   1. Replace the contents of the credential file:
#        op read "op://video-factory/<name>" > /etc/video-factory/credentials/<name>
#   2. systemctl restart video-factory.service
#   The new value is loaded by ExecStartPre=video-factory-load-env.sh
#   before the next compose up. No downtime beyond the docker restart
#   window.
#
# DB key rotation
# ---------------
# VIDEO_FACTORY_DB_KEY rotation is NOT handled by this file. It is a
# stateful change that re-encrypts the DB through the owner-only admin
# endpoint (Wave 5, control_plane/key_rotation.py):
#   GET  /api/v1/admin/keys/status   rotation state
#   POST /api/v1/admin/keys/rotate   {"old_key": ..., "new_key": ...}
# Step-by-step (curl + owner session cookie): docs/operations-prod.md,
# "Rotate the DB master key (VIDEO_FACTORY_DB_KEY)". There is no scripts/rotate_master_key.sh.
# After a successful rotation write the new value into
# /etc/video-factory/credentials/video_factory_db_key (atomic rename)
# and restart the service so all processes see the new key.
#
# Enable Sentry in production
# --------------------------
# Sentry is opt-in. Three credentials are recognised by the loader on
# top of the required seven, all OPTIONAL. Drop a file into the same
# directory used by `LoadCredential=`:
#   /etc/video-factory/credentials/sentry_dsn          mode 0600, root:root
#   /etc/video-factory/credentials/sentry_environment  mode 0600, root:root   (optional)
#   /etc/video-factory/credentials/sentry_release      mode 0600, root:root   (optional)
#
# To enable:
#   1. Decide the values. `sentry_dsn` is the DSN from the Sentry
#      project settings page. `sentry_environment` should match the
#      deploy (e.g. `production`). `sentry_release` should match the
#      image tag or git SHA shipped to the host.
#   2. Populate:
#        op read "op://video-factory/sentry_dsn" > /etc/video-factory/credentials/sentry_dsn
#        op read "op://video-factory/sentry_environment" > /etc/video-factory/credentials/sentry_environment
#        op read "op://video-factory/sentry_release" > /etc/video-factory/credentials/sentry_release
#      OR equivalent (sops decrypt, hand-paste for one-off, etc.).
#      Permissions match the rest of the directory:
#        install -m 0600 /dev/null /etc/video-factory/credentials/sentry_dsn
#        chown root:root /etc/video-factory/credentials/sentry_dsn
#   3. systemctl restart video-factory.service. The loader renders the
#      new values into /opt/video-factory/.env.production before the
#      next compose up.
#
# Resulting lines in .env.production (values shown only as shapes):
#
#   # Generated by video-factory-load-env.sh on 2026-10-10T07:24:30Z
#   # DO NOT edit by hand. DO NOT commit. Contents come from systemd
#   # LoadCredential= entries in video-factory.service.
#
#   SENTRY_DSN='https://***@o000000.ingest.sentry.io/0000000'
#   SENTRY_ENVIRONMENT='production'
#   SENTRY_RELEASE='v0.0.0'
#
# Absent-credential behaviour: if a sentry_* file is missing OR empty,
# the loader SKIPS it. .env.production then contains NO `SENTRY_*=`
# line for that variable at all — not an empty value, not a commented
# placeholder. `docker compose` sees the variable as unset and falls
# back to the `${VAR:-}` defaults in docker-compose.production.yml,
# which keeps `sentry_sdk` uninitialised (Sentry off). The seven
# required credentials remain fail-fast on missing/empty — only the
# three sentry_* entries are skipped.
#
# Operator-facing context (PII redaction policy, init_sentry() options,
# what the SDK does on each event): docs/observability.md,
# section "Errors and Sentry".
#
# This is the ONLY supported route. Do not:
#   - edit /opt/video-factory/.env.production by hand. The loader
#     rewrites it on every start (see `> "$ENV_FILE"` at the top of
#     video-factory-load-env.sh). Hand edits are wiped on restart.
#   - add a systemd drop-in `Environment=SENTRY_DSN=...`. That bypass
#     keeps the value out of the credential audit trail, sets the
#     wrong mode (unit env is world-readable to the unit's user, not
#     mode 0600), and is undocumented on purpose.
#
# To turn Sentry off again, delete (or `> /etc/video-factory/credentials/sentry_dsn`)
# the credential file(s) and restart the service. The next .env.production
# loses the line. Rotation of the DSN itself is the usual credential
# rotation from the `Rotation` section above.
#
# Audit
# -----
# Every credential load goes through ExecStartPre=video-factory-load-env.sh,
# which appends to /var/log/video-factory/credential-loads.log with
# (timestamp, credential_name, source). The credential VALUE is never
# logged.
#
# Compliance checks (operator self-audit, monthly)
# ------------------------------------------------
# - All credential files are mode 0600, owned by root:root
# - No .env, .env.local, or .env.production file exists in the repo
#   working tree (git status verify)
# - git log -p -- .env.production shows zero history
# - The Caddyfile / openapi.json / docs NEVER mention a credential
#   by its name (only by env var)