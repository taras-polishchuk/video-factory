#!/usr/bin/env bash
# remote-bootstrap.sh: idempotent server bootstrap, run ON the production
# host as root by provision-and-deploy.sh (Ubuntu 24.04, Hetzner image).
#
#   bash /opt/video-factory/scripts/remote-bootstrap.sh
#
# Installs what the deploy path needs and nothing else:
#   - docker engine + compose plugin (Ubuntu packages docker.io +
#     docker-compose-v2) unless docker + `docker compose` already exist
#   - restic (backups, A2 F-22/F-28) and rsync
#   - /etc/video-factory/{,credentials/} with mode 0700
#   - systemd units: video-factory.service, video-factory-backup.{service,timer}
#     and /usr/local/bin/video-factory-load-env.sh
# The backup timer is enabled only when /etc/video-factory/backup.env
# (restic repo + password + S3 creds, operator-provided) exists.

set -euo pipefail

SRC="${VF_SRC_DIR:-/opt/video-factory}"
ETC=/etc/video-factory

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
pkgs=()
command -v docker >/dev/null 2>&1 || pkgs+=(docker.io)
if ! docker compose version >/dev/null 2>&1; then pkgs+=(docker-compose-v2); fi
command -v restic >/dev/null 2>&1 || pkgs+=(restic)
command -v rsync >/dev/null 2>&1 || pkgs+=(rsync)
command -v curl >/dev/null 2>&1 || pkgs+=(curl)
if [ "${#pkgs[@]}" -gt 0 ]; then
  echo "bootstrap: apt install ${pkgs[*]}"
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends "${pkgs[@]}"
fi
systemctl enable --now docker >/dev/null
docker compose version >/dev/null
restic version >/dev/null

install -d -m 0700 -o root -g root "$ETC" "$ETC/credentials"
install -d -m 0750 -o root -g root /var/backups/video-factory

install -m 0644 -o root -g root "$SRC/systemd/video-factory.service" /etc/systemd/system/video-factory.service
install -m 0644 -o root -g root "$SRC/systemd/video-factory-backup.service" /etc/systemd/system/video-factory-backup.service
install -m 0644 -o root -g root "$SRC/systemd/video-factory-backup.timer" /etc/systemd/system/video-factory-backup.timer
install -m 0755 -o root -g root "$SRC/systemd/video-factory-load-env.sh" /usr/local/bin/video-factory-load-env.sh
systemctl daemon-reload

if [ -f "$ETC/backup.env" ]; then
  chmod 0600 "$ETC/backup.env"
  systemctl enable --now video-factory-backup.timer >/dev/null
  echo "bootstrap: backup timer enabled"
else
  echo "bootstrap: $ETC/backup.env missing; backup timer NOT enabled (see docs/operations-prod.md)"
fi

echo "bootstrap: OK (docker $(docker version --format '{{.Server.Version}}'), $(restic version | head -n 1))"
