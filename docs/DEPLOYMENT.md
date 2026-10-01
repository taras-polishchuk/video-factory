# Deployment Guide

This guide covers self-hosted deployment of the Video Factory Control
Room on a single VPS. It assumes the operator wants a working
multi-company environment with mock-mode rendering; live HeyGen /
ComfyUI / Publer integrations are out of scope for this guide.

> **Bind to localhost / private network only.** The default config has
> no authentication. Do not expose the API or UI to the public
> internet without adding auth + TLS + a reverse proxy first.

## Minimum hardware

For a single-workspace deployment running in mock mode with FFmpeg
assembly on demand:

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB | 40 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS |
| Public bandwidth | 10 Mbps | 50 Mbps |
| Static IPv4 | Yes (for HTTPS) | Yes |

Why RAM matters: FFmpeg assembly with `drawtext` + audio mix for a
single 30 s clip at 1080x1920 peaks at ~400 MB resident. A job
runner handling concurrent submissions should plan 1 GB headroom
above steady-state. Add ~2 GB if you plan to enable live HeyGen
rendering later (their worker pods run alongside).

## VPS providers tested in spirit (cheap enough to start)

| Provider | Plan | Spec | Region | Notes |
|---|---|---|---|---|
| Hetzner | CX22 | 2 vCPU, 4 GB, 40 GB NVMe | Falkenstein / Helsinki | €4.85/mo, IPv4 included |
| Hetzner | CX32 | 4 vCPU, 8 GB, 80 GB NVMe | same | €8.21/mo, room for live providers |
| Netcup | VPS 1000 | 4 vCPU, 8 GB, 80 GB NVMe | Germany | €8.50/mo |
| DigitalOcean | Basic Droplet | 2 vCPU, 4 GB, 80 GB | AMS / NYC / SFO | $24/mo |
| Vultr | Regular | 2 vCPU, 4 GB, 80 GB | many | $24/mo |
| OVH | Starter | 1 vCPU, 2 GB, 20 GB | GRA | €3.50/mo — **below minimum** |

For the local demo (no live providers, no GPU) the Hetzner CX22 or
DigitalOcean Basic Droplet is plenty.

## Architecture on the VPS

```
┌──────────── VPS (single host, 127.0.0.1) ─────────────┐
│                                                    │
│  ┌── docker compose stack ──┐                       │
│  │                          │                       │
│  │  api  :8000  (FastAPI)   │                       │
│  │  web  :5173  (Vite dev)   │                       │
│  │  data volume             │                       │
│  │                          │                       │
│  └──────────────────────────┘                       │
│           ▲                                         │
│           │ /api proxy                              │
│  ┌── Caddy / nginx ────────┐  optional, when        │
│  │  reverse proxy +       │  serving from internet       │
│  │  Let's Encrypt TLS      │                       │
│  └─────────────────────────┘                       │
│                                                    │
└────────────────────────────────────────────────────┘
```

The default `docker-compose.yml` binds both services to `127.0.0.1`.
Add a reverse proxy only when you have decided on auth + TLS.

## One-line setup (manual mode)

```bash
# 1. Clone the repo
git clone https://github.com/taras-polishchuk/video-factory.git
cd video-factory

# 2. Install OS-level deps (ffmpeg, fonts for drawtext)
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv \
                        nodejs npm ffmpeg fonts-liberation

# 3. Python deps
python3 -m pip install --user 'jsonschema>=4.0' 'fastapi>=0.115' \
                            'uvicorn>=0.30' 'python-multipart>=0.0.9'

# 4. Verify
PYTHONPATH=. python3 -m pytest tests/ -q          # 50 passed

# 5. Start the stack (API on 8000, UI on 5173)
./scripts/start-local.sh
```

The script installs missing Python deps, creates `artifacts/` for
SQLite + storage, runs API + UI in the foreground. `Ctrl+C` stops
both.

## One-line setup (Docker Compose)

```bash
sudo apt-get install -y docker.io docker-compose-plugin
git clone https://github.com/taras-polishchuk/video-factory.git
cd video-factory
docker compose up
```

The compose file mounts the repo into the api container and a named
volume (`vf_data`) for SQLite + storage. Stop with `docker compose down`.
To reset state: `docker compose down -v`.

## Production deployment checklist

- [ ] Pick a VPS (Hetzner CX22 or similar, 2 vCPU / 4 GB).
- [ ] Provision with Ubuntu 22.04 LTS.
- [ ] Create a non-root `taras` user, disable root SSH login.
- [ ] Configure UFW: allow SSH (22), HTTPS (443), block everything else.
- [ ] Install Docker or Python+Node directly (whichever you prefer).
- [ ] Clone the repo to `/opt/video-factory`.
- [ ] Set up systemd unit for the stack (see below).
- [ ] Decide on TLS termination: Caddy (recommended, automatic HTTPS)
      or nginx + certbot.
- [ ] **Before exposing:** add bearer-token auth or front it with
      Cloudflare Access / Authentik / Tailscale. The default install
      binds to localhost.
- [ ] Set up backups for `artifacts/` (SQLite + storage).
- [ ] Configure log rotation: `/var/log/video-factory/*.log` → logrotate.
- [ ] Add monitoring: health-check endpoint at `/health`.

## systemd unit (manual mode)

`/etc/systemd/system/video-factory.service`:

```ini
[Unit]
Description=Video Factory Control Room
After=network.target

[Service]
Type=simple
User=taras
WorkingDirectory=/opt/video-factory
Environment=PYTHONPATH=/opt/video-factory
Environment=VIDEO_FACTORY_DB=/opt/video-factory/artifacts/video_factory.db
Environment=VIDEO_FACTORY_STORAGE=/opt/video-factory/artifacts/storage
Environment=MAX_COST_PER_VIDEO_USD=5
Environment=MAX_COST_PER_BATCH_USD=20
ExecStart=/opt/video-factory/scripts/start-local.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now video-factory
sudo systemctl status video-factory
```

## Reverse proxy (Caddy example)

`/etc/caddy/Caddyfile`:

```
video-factory.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:5173
}
```

The web service proxies `/api` to the API service on `:8000`, so a
single Caddy entry is enough.

For nginx:

```nginx
server {
    server_name video-factory.example.com;
    listen 80;

    location / {
        proxy_pass http://127.0.0.1:5173;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
    }
}
```

Run `certbot --nginx -d video-factory.example.com` after the initial
deploy.

## Backup strategy

The state lives in two places:

| Data | Path | Backup |
|---|---|---|
| SQLite + idempotency | `artifacts/video_factory.db`, `artifacts/storage/idem.db` | Daily snapshot via cron |
| Uploaded assets | `artifacts/storage/companies/<slug>/assets/` | Same snapshot |
| Generated outputs | `artifacts/storage/jobs/<job_id>/` | Same snapshot |

Recommended cron (with restic or borg):

```cron
0 3 * * * taras /usr/local/bin/restic backup /opt/video-factory/artifacts
```

## Scaling notes (V0 is single-host)

The current implementation assumes a single-process job runner. To
scale beyond one host:

- Move SQLite to a Postgres backend (the `core/repo.py` surface is
  small enough to swap).
- Move file storage to S3 (or compatible) using a new `S3Storage`
  against the existing `Storage` protocol — the API surface does not
  change.
- Run multiple API replicas behind a load balancer; the
  `JobRunner` is process-local, so for true horizontal scaling you
  need a queue backend (Redis, RabbitMQ, SQS) and per-worker
  lease semantics.

These are documented as future missions in
`/home/taras/projects/.project-state/video-factory-ui-2026-10-01/final-report.md`.

## Operating limits (current V0)

- One SQLite database → vertical scaling only.
- Job runner is single-process → single-node scaling.
- File storage on local disk → backup the volume, not a shared FS.
- No auth → bind to localhost / private network only.

These are stated explicitly in the project so the next engineer does
not need to rediscover them.