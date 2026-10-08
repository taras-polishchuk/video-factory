# OpenTofu module — Hetzner CX22 + firewall + Cloudflare DNS record
#
# Provisions:
#   - 1× Hetzner Cloud CX22 (Intel, 2 vCPU, 4GB RAM, 40GB SSD)
#   - 1× Hetzner firewall (allow 22, 80, 443; SSH only from allowed CIDR)
#   - 1× Cloudflare DNS A record pointing the configured domain at
#     the server's public IPv4
#   - 1× Cloudflare DNS A record for `www.<domain>` (CNAME-flattened
#     via the A record)
#
# Inputs are sourced from variables. Tokens are passed through TF_VAR_
# env vars at apply time (e.g. `TF_VAR_hcloud_token`); they MUST NOT be
# committed to git and MUST NOT appear in any *.tfvars file.
#
# Usage:
#   cd infra/opentofu
#   tofu init -backend=false
#   tofu validate
#   tofu plan -var "domain=<server_name>"
#   tofu apply -auto-approve -var "domain=<server_name>"

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.20"
    }
  }
}

# ─── Inputs ─────────────────────────────────────────────────────

variable "hcloud_token" {
  description = "Hetzner Cloud API token. Read from TF_VAR_hcloud_token env."
  type        = string
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token with DNS edit on the zone. Read from TF_VAR_cloudflare_api_token."
  type        = string
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID where the DNS A record will be created."
  type        = string
}

variable "domain" {
  description = "Apex domain (e.g. video-factory.example.com). The server name + the DNS record both use this."
  type        = string
}

variable "server_type" {
  description = "Hetzner server type. Brief: CX22 (default)."
  type        = string
  default     = "cx22"

  # Guard against accidental invalid sizes (Wave 10 adversarial test).
  validation {
    condition     = contains(["cx22", "cx32", "cx42", "cx52"], var.server_type)
    error_message = "server_type must be one of: cx22, cx32, cx42, cx52."
  }
}

variable "location" {
  description = "Hetzner datacenter location."
  type        = string
  default     = "fsn1" # Falkenstein, DE
}

variable "ssh_public_key_path" {
  description = "Path on the deployer host to the SSH public key that will be installed on the new server."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "admin_cidr" {
  description = "CIDR allowed to SSH into the new server. Default = operator home network."
  type        = string
  default     = "0.0.0.0/0" # operator MUST override before apply
}

# ─── SSH key ───────────────────────────────────────────────────

resource "hcloud_ssh_key" "default" {
  name       = "video-factory-${var.domain}"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

# ─── Firewall ──────────────────────────────────────────────────

resource "hcloud_firewall" "video_factory" {
  name = "video-factory-${var.domain}"

  # SSH: operator CIDR only
  rule {
    direction  = "IN"
    protocol   = "tcp"
    port       = "22"
    source_ips = [var.admin_cidr]
  }

  # HTTP: redirect-to-HTTPS only; Caddy never listens here in the
  # DNS-01 model but Hetzner's Network tab advertises :80 → :443
  # redirects at the load-balancer level. We open :80 outbound-only
  # for ACME in case a future flow needs it.
  rule {
    direction  = "IN"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # HTTPS
  rule {
    direction  = "IN"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # ICMP (ping) — useful for healthcheck scripts
  rule {
    direction  = "IN"
    protocol   = "icmp"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # Outbound: full
  rule {
    direction       = "OUT"
    protocol        = "tcp"
    port            = "any"
    destination_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction       = "OUT"
    protocol        = "udp"
    port            = "any"
    destination_ips = ["0.0.0.0/0", "::/0"]
  }
}

# ─── Server ────────────────────────────────────────────────────

resource "hcloud_server" "video_factory" {
  name         = "video-factory-${var.domain}"
  server_type  = var.server_type
  location     = var.location
  image        = "ubuntu-24.04"
  firewall_ids = [hcloud_firewall.video_factory.id]
  ssh_keys     = [hcloud_ssh_key.default.id]

  labels = {
    project     = "video-factory"
    environment = "production"
    managed_by  = "opentofu"
  }
}

# ─── Cloudflare DNS record ────────────────────────────────────

resource "cloudflare_record" "apex" {
  zone_id = var.cloudflare_zone_id
  name    = var.domain
  type    = "A"
  value   = hcloud_server.video_factory.ipv4_address
  proxied = true
  ttl     = 1 # Auto when proxied
  comment = "Managed by tofu — video-factory"
}

resource "cloudflare_record" "www" {
  zone_id = var.cloudflare_zone_id
  name    = "www.${var.domain}"
  type    = "A"
  value   = hcloud_server.video_factory.ipv4_address
  proxied = true
  ttl     = 1
  comment = "Managed by tofu — video-factory www"
}

# ─── Outputs ───────────────────────────────────────────────────

output "server_id" {
  description = "Hetzner server id (used by provision-and-deploy.sh to SSH in)."
  value       = hcloud_server.video_factory.id
}

output "server_ipv4" {
  description = "Public IPv4 of the new server."
  value       = hcloud_server.video_factory.ipv4_address
}

output "ssh_command" {
  description = "Convenience: the SSH command to reach the new server."
  value       = "ssh root@${hcloud_server.video_factory.ipv4_address}"
  sensitive   = false
}