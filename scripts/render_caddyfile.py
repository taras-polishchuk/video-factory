#!/usr/bin/env python3
"""Render the per-domain Caddyfile from the tracked template (A2 F-33).

The tracked ./Caddyfile is a template and is never modified. This script
writes the rendered copy (default: infra/hosts/<domain>/Caddyfile, which
is gitignored) and docker-compose.production.yml mounts it through
VIDEO_FACTORY_CADDYFILE.

Usage:
  scripts/render_caddyfile.py <domain> [--out PATH] [--acme-email EMAIL]
                              [--tls-internal] [--template PATH]

--tls-internal replaces the Cloudflare DNS-01 tls block with Caddy's
internal CA. Only for local smoke tests / drills where no real
certificate can be issued; never for production.

The domain is validated strictly: it ends up as a Caddy site address
and as a directory name, so anything outside [a-z0-9.-] is refused
(no Caddyfile injection, no path traversal).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "Caddyfile"
PLACEHOLDER = "{$VIDEO_FACTORY_DOMAIN:video-factory.example.com}"

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
DOMAIN_RE = re.compile(rf"^(?=.{{1,253}}$)(?:{_LABEL}\.)+[a-z]{{2,63}}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
TLS_BLOCK_RE = re.compile(r"^(?P<indent>[ \t]*)tls \{\n(?:.*\n)*?(?P=indent)\}\n", re.MULTILINE)
GLOBAL_OPEN_RE = re.compile(r"^\{\n", re.MULTILINE)


class RenderError(ValueError):
    pass


def validate_domain(domain: str) -> str:
    d = domain.strip()
    if d != d.lower() or not DOMAIN_RE.match(d):
        raise RenderError(
            f"invalid domain {domain!r}: expected a lowercase hostname like "
            "video.example.com (letters, digits, '-', '.')"
        )
    return d


def render(template_text: str, domain: str, *, acme_email: str | None = None,
           tls_internal: bool = False) -> str:
    domain = validate_domain(domain)
    if PLACEHOLDER not in template_text:
        raise RenderError(f"template has no {PLACEHOLDER} placeholder")
    out = template_text.replace(PLACEHOLDER, domain)
    leftover = [ln for ln in out.splitlines()
                if "{$VIDEO_FACTORY_DOMAIN" in ln and not ln.lstrip().startswith("#")]
    if leftover:
        raise RenderError(
            f"template has an unexpected VIDEO_FACTORY_DOMAIN placeholder form: {leftover[0]!r}"
        )

    global_opts: list[str] = []
    if acme_email:
        if not EMAIL_RE.match(acme_email):
            raise RenderError(f"invalid ACME email {acme_email!r}")
        global_opts.append(f"\temail {acme_email}")
    if tls_internal:
        out, n = TLS_BLOCK_RE.subn(lambda m: f"{m.group('indent')}tls internal\n", out)
        if n != 1:
            raise RenderError(f"expected exactly one tls block in the template, found {n}")
        global_opts.append("\tskip_install_trust")
    if global_opts:
        out, n = GLOBAL_OPEN_RE.subn("{\n" + "\n".join(global_opts) + "\n", out, count=1)
        if n != 1:
            raise RenderError("template has no global options block")

    mode = "tls internal (TEST ONLY)" if tls_internal else "Cloudflare DNS-01"
    header = (
        f"# RENDERED for {domain} by scripts/render_caddyfile.py ({mode}).\n"
        "# Do not edit: change the tracked ./Caddyfile template and re-render.\n"
    )
    return header + out


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".Caddyfile.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render the per-domain Caddyfile")
    ap.add_argument("domain")
    ap.add_argument("--out", type=Path, default=None,
                    help="output path (default: infra/hosts/<domain>/Caddyfile)")
    ap.add_argument("--template", type=Path, default=TEMPLATE)
    ap.add_argument("--acme-email", default=None)
    ap.add_argument("--tls-internal", action="store_true",
                    help="use Caddy's internal CA (local smoke tests only)")
    args = ap.parse_args(argv)

    try:
        domain = validate_domain(args.domain)
        out_path = args.out or (ROOT / "infra" / "hosts" / domain / "Caddyfile")
        if out_path.resolve() == args.template.resolve():
            raise RenderError("refusing to overwrite the template (A2 F-33)")
        text = render(args.template.read_text(), domain,
                      acme_email=args.acme_email, tls_internal=args.tls_internal)
    except RenderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    write_atomic(out_path, text)
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
