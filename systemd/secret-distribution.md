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
# stateful change that requires running
# scripts/rotate_master_key.sh (Wave 5 deliverable, idempotent).
# Once the rotation finishes, restart the service so all processes see
# the new key.
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