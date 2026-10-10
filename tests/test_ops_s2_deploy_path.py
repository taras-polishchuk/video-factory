"""OPS S2: production deploy path contracts (A2 F-02/06/08/18/21/24/26/27/33).

Static + subprocess checks that need no docker, tofu or caddy:

  * .env.example documents every VIDEO_FACTORY_* / VF_* / STRIPE_* name
    the code, the shell scripts and the production compose file read.
  * scripts/render_caddyfile.py renders per domain, never mutates the
    tracked template, refuses injection, keeps /metrics closed.
  * scripts/deploy.sh refuses APPLY=1 with SSH open to the internet
    unless ALLOW_OPEN_SSH=1; provision-and-deploy.sh gates on
    `tofu plan -detailed-exitcode`.
  * requirements.lock matches pyproject + the tested venv; Dockerfiles
    pin base images by digest; compose carries the hardening knobs.
  * the legacy Stripe price env names still work, with a warning.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
ENV_NAME = re.compile(r"^(?:VIDEO_FACTORY|VF|STRIPE)_[A-Z0-9_]+$")
ENV_REF = r"(?:VIDEO_FACTORY|VF|STRIPE)_[A-Z0-9_]+"

# Python packages that make up the deployed server (tests/ excluded).
CODE_DIRS = ("api", "auth", "control_plane", "core", "providers", "scripts")


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ────────────────────────────────────────────────────────────────────
# 1) env var documentation parity (A2 F-24)
# ────────────────────────────────────────────────────────────────────
def _documented_env_names() -> set[str]:
    text = (REPO_ROOT / ".env.example").read_text()
    return set(re.findall(rf"^#?\s*({ENV_REF})=", text, re.MULTILINE))


def _env_names_read_by_code() -> dict[str, set[str]]:
    """Every env-shaped string literal in server code, shell, compose, web.

    String constants (not just os.environ.get("X") calls) so indirection
    like ``STRIPE_SECRET_KEY_ENV = "STRIPE_SECRET_KEY"`` is caught too.
    """
    found: dict[str, set[str]] = {}

    def add(name: str, where: Path) -> None:
        found.setdefault(name, set()).add(str(where.relative_to(REPO_ROOT)))

    for d in CODE_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            tree = ast.parse(p.read_text(), filename=str(p))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and ENV_NAME.match(node.value)):
                    add(node.value, p)

    shell = sorted(SCRIPTS.glob("*.sh")) + sorted((REPO_ROOT / "systemd").glob("*.sh"))
    shell.append(REPO_ROOT / "docker-compose.production.yml")
    for p in shell:
        for m in re.finditer(rf"\$\{{?({ENV_REF})", p.read_text()):
            add(m.group(1), p)

    for p in (REPO_ROOT / "web" / "src").rglob("*"):
        if p.suffix in (".ts", ".js", ".svelte"):
            for m in re.finditer(rf"\b(?:env|process\.env)\.({ENV_REF})", p.read_text()):
                add(m.group(1), p)
    return found


def test_env_scanner_sees_known_names():
    """Guard against a scanner that silently finds nothing."""
    names = _env_names_read_by_code()
    for expected in ("VIDEO_FACTORY_DB_KEY", "VIDEO_FACTORY_SECRET_KEY",
                     "STRIPE_SECRET_KEY", "STRIPE_PRO_PRICE_ID",
                     "VIDEO_FACTORY_API_URL"):
        assert expected in names, f"scanner lost {expected}"


def test_env_example_documents_every_env_var_the_code_reads():
    documented = _documented_env_names()
    undocumented = {k: sorted(v) for k, v in _env_names_read_by_code().items()
                    if k not in documented}
    assert not undocumented, (
        "env vars read by code but missing from .env.example "
        "(add a line `NAME=` or `# NAME=` with a comment): "
        f"{undocumented}"
    )


def test_env_example_has_no_secret_values():
    """Secrets in .env.example must be empty placeholders."""
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        m = re.match(r"^#?\s*([A-Z0-9_]*(?:SECRET|_KEY|TOKEN|PASSWORD))=(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip().strip('"').strip("'")
        assert value == "" or value.startswith("<") or "change-me" in value.lower(), (
            f".env.example carries a value for {m.group(1)}"
        )


# ────────────────────────────────────────────────────────────────────
# 2) Caddyfile rendering (A2 F-27, F-33; staging VF-STG-06/07)
# ────────────────────────────────────────────────────────────────────
def test_render_caddyfile_substitutes_domain_and_keeps_template(tmp_path):
    template = REPO_ROOT / "Caddyfile"
    before = hashlib.sha256(template.read_bytes()).hexdigest()
    out = tmp_path / "hosts" / "vf.example.org" / "Caddyfile"
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_caddyfile.py"), "vf.example.org",
         "--out", str(out), "--acme-email", "ops@example.org"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    assert hashlib.sha256(template.read_bytes()).hexdigest() == before, \
        "render must never modify the tracked Caddyfile"
    text = out.read_text()
    assert "{$VIDEO_FACTORY_DOMAIN" not in text
    assert re.search(r"^vf\.example\.org \{$", text, re.MULTILINE)
    assert re.search(r"^http://vf\.example\.org \{$", text, re.MULTILINE)
    assert "dns cloudflare {env.CLOUDFLARE_API_TOKEN}" in text
    assert "\temail ops@example.org" in text
    assert text.count("{") == text.count("}")


def test_rendered_caddyfile_routes_and_closes_metrics():
    mod = _load_script("render_caddyfile")
    text = mod.render((REPO_ROOT / "Caddyfile").read_text(), "vf.example.org")
    api_line = next(ln for ln in text.splitlines() if ln.strip().startswith("@api "))
    for path in ("/api/*", "/auth/*", "/healthz", "/readyz"):
        assert path in api_line.split(), f"{path} must be proxied to the API"
    assert re.search(r"@metrics path /metrics", text), "/metrics must be matched"
    block = text[text.index("handle @metrics"):]
    assert "respond 404" in block.split("}")[0], "/metrics must not be public"
    assert "reverse_proxy api:8000" in text and "reverse_proxy web:3000" in text


def test_render_caddyfile_tls_internal_drops_dns_provider():
    mod = _load_script("render_caddyfile")
    text = mod.render((REPO_ROOT / "Caddyfile").read_text(), "vf.example.org",
                      tls_internal=True)
    live = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "dns cloudflare" not in live
    assert "\ttls internal\n" in text
    assert "TEST ONLY" in text


@pytest.mark.parametrize("bad", [
    "Example.com",                   # upper case
    "vf.example.org {\n respond x",  # Caddyfile injection
    "../../etc",                     # path traversal
    "vf.example.org/x",
    "-vf.example.org",
    "localhost",                     # no TLD
    "",
])
def test_render_caddyfile_rejects_bad_domains(tmp_path, bad):
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_caddyfile.py"), bad,
         "--out", str(tmp_path / "Caddyfile")],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 2, (bad, r.stdout, r.stderr)
    assert not (tmp_path / "Caddyfile").exists()


def test_render_caddyfile_refuses_to_overwrite_template(tmp_path):
    template = tmp_path / "Caddyfile"
    shutil.copy(REPO_ROOT / "Caddyfile", template)
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_caddyfile.py"), "vf.example.org",
         "--template", str(template), "--out", str(template)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 2
    assert template.read_text() == (REPO_ROOT / "Caddyfile").read_text()


def test_caddy_image_builds_cloudflare_module():
    dockerfile = (REPO_ROOT / "infra" / "caddy" / "Dockerfile").read_text()
    assert "xcaddy build" in dockerfile
    assert "github.com/caddy-dns/cloudflare" in dockerfile
    for m in re.finditer(r"^ARG \w+_IMAGE=(\S+)$", dockerfile, re.MULTILINE):
        assert "@sha256:" in m.group(1), f"unpinned base image {m.group(1)}"


# ────────────────────────────────────────────────────────────────────
# 3) deploy.sh / provision guards (A2 F-06)
# ────────────────────────────────────────────────────────────────────
DUMMY_DEPLOY_ENV = {
    "HETZNER_TOKEN": "dummy-hcloud",
    "CLOUDFLARE_API_TOKEN": "dummy-cf",
    "CLOUDFLARE_ZONE_ID": "dummy-zone",
    "STRIPE_SECRET_KEY": "sk_test_dummy",
    "STRIPE_WEBHOOK_SECRET": "whsec_dummy",
    "VIDEO_FACTORY_SECRET_KEY": "dummy-fernet",
    "VIDEO_FACTORY_DB_KEY": "dummy-db-key",
    "VIDEO_FACTORY_SESSION_SECRET": "dummy-session",
}


@pytest.fixture
def deploy_tree(tmp_path):
    """Copy of the deploy entry point with a stub provision script.

    The stub exits 42 so a run that gets PAST the guards is visible
    (exit 42) and never reaches tofu, ssh or the smoke check.
    """
    (tmp_path / "scripts").mkdir()
    for name in ("deploy.sh", "render_caddyfile.py"):
        shutil.copy2(SCRIPTS / name, tmp_path / "scripts" / name)
    shutil.copy2(REPO_ROOT / "Caddyfile", tmp_path / "Caddyfile")
    stub = tmp_path / "scripts" / "provision-and-deploy.sh"
    stub.write_text("#!/usr/bin/env bash\necho \"STUB provision $*\"\nexit 42\n")
    stub.chmod(0o755)
    return tmp_path


def _run_deploy(tree: Path, *args: str, **env_over: str):
    env = {"PATH": os.environ["PATH"], "HOME": str(tree), **DUMMY_DEPLOY_ENV}
    env.update(env_over)
    return subprocess.run(
        ["bash", str(tree / "scripts" / "deploy.sh"), "vf.example.org", *args],
        capture_output=True, text=True, env=env, cwd=str(tree), timeout=60,
    )


@pytest.mark.parametrize("cidr", [None, "0.0.0.0/0", "::/0"])
def test_deploy_refuses_apply_with_open_ssh(deploy_tree, cidr):
    over = {"APPLY": "1"}
    if cidr is not None:
        over["ADMIN_CIDR"] = cidr
    r = _run_deploy(deploy_tree, **over)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "refusing APPLY=1" in r.stderr
    assert "STUB provision" not in r.stdout


def test_deploy_allow_open_ssh_is_an_explicit_opt_in(deploy_tree):
    r = _run_deploy(deploy_tree, APPLY="1", ADMIN_CIDR="0.0.0.0/0", ALLOW_OPEN_SSH="1")
    assert r.returncode == 42, (r.stdout, r.stderr)
    assert "STUB provision vf.example.org --apply" in r.stdout
    assert "WARNING" in r.stderr


def test_deploy_narrow_cidr_passes_guard(deploy_tree):
    r = _run_deploy(deploy_tree, APPLY="1", ADMIN_CIDR="203.0.113.7/32")
    assert r.returncode == 42, (r.stdout, r.stderr)
    assert "STUB provision vf.example.org --apply" in r.stdout
    host_env = (deploy_tree / "infra" / "hosts" / "vf.example.org.env").read_text()
    assert "ADMIN_CIDR=203.0.113.7/32" in host_env
    for secret in DUMMY_DEPLOY_ENV.values():
        assert secret not in host_env, "secrets must not land in the host env file"
    rendered = deploy_tree / "infra" / "hosts" / "vf.example.org" / "Caddyfile"
    assert rendered.is_file()
    assert (deploy_tree / "Caddyfile").read_text() == (REPO_ROOT / "Caddyfile").read_text()


def test_deploy_plan_only_with_open_ssh_is_allowed(deploy_tree):
    """Without APPLY=1 nothing is created, so the guard only warns."""
    r = _run_deploy(deploy_tree)
    assert r.returncode == 42, (r.stdout, r.stderr)
    assert "STUB provision vf.example.org" in r.stdout
    assert "--apply" not in r.stdout


def test_deploy_rejects_invalid_cidr(deploy_tree):
    r = _run_deploy(deploy_tree, APPLY="1", ADMIN_CIDR="not-a-cidr")
    assert r.returncode == 2, (r.stdout, r.stderr)


def test_deploy_rejects_bad_apply_value(deploy_tree):
    r = _run_deploy(deploy_tree, APPLY="yes")
    assert r.returncode == 2


def test_provision_gates_on_detailed_exitcode():
    text = (SCRIPTS / "provision-and-deploy.sh").read_text()
    assert re.search(r"tofu plan [^\n]*-detailed-exitcode", text)
    assert re.search(r"\*\)\s*echo \"ERROR: tofu plan failed", text), \
        "plan exit codes other than 0/2 must abort"
    assert "tofu apply" in text and '"$PLAN_FILE"' in text, \
        "apply must use the saved plan file"


# ────────────────────────────────────────────────────────────────────
# 4) image deps + base pins + compose hardening (A2 F-08/18/21/25/26)
# ────────────────────────────────────────────────────────────────────
def test_requirements_lock_matches_venv_and_pyproject():
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "gen_requirements_lock.py"), "--check"],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_requirements_lock_is_hashed():
    text = (REPO_ROOT / "requirements.lock").read_text()
    pins = re.findall(r"^[A-Za-z0-9_.-]+==\S+", text, re.MULTILINE)
    assert pins
    assert text.count("--hash=sha256:") >= len(pins)


def test_api_dockerfile_installs_from_lock_and_pins_base():
    text = (REPO_ROOT / "Dockerfile").read_text()
    assert re.search(r"^FROM \S+@sha256:[0-9a-f]{64}", text, re.MULTILINE)
    assert "--require-hashes" in text and "requirements.lock" in text
    assert '"--host", "0.0.0.0"' in text, "uvicorn must listen on the container network"
    assert "api/uvicorn_log_config.json" in text
    assert re.search(r"^USER \S+", text, re.MULTILINE)


def test_web_dockerfile_is_multistage_non_root_adapter_node():
    text = (REPO_ROOT / "web" / "Dockerfile").read_text()
    assert len(re.findall(r"^FROM ", text, re.MULTILINE)) >= 2
    assert re.search(r"^USER node$", text, re.MULTILINE)
    assert "@sha256:" in text
    assert "@sveltejs/adapter-node" in (REPO_ROOT / "web" / "svelte.config.js").read_text()
    assert "@sveltejs/adapter-node" in (REPO_ROOT / "web" / "package.json").read_text()


def _compose_service(name: str) -> str:
    """Text of one service block, with `<<: *anchor` merges inlined.

    No PyYAML in the venv, so resolve the x-* anchors by hand: the
    anchor body is re-indented to service level and appended.
    """
    text = (REPO_ROOT / "docker-compose.production.yml").read_text()
    m = re.search(rf"^  {name}:\n((?:    .*\n|\s*\n)+)", text, re.MULTILINE)
    assert m, f"service {name} missing"
    block = m.group(1)
    for ref in re.findall(r"^    <<: \*(\S+)$", block, re.MULTILINE):
        a = re.search(rf"^x-[\w-]+: &{re.escape(ref)}\n((?:  .*\n|\s*\n)+)", text, re.MULTILINE)
        assert a, f"anchor {ref} missing"
        block += "".join("  " + ln + "\n" for ln in a.group(1).splitlines())
    return block


@pytest.mark.parametrize("svc", ["api", "web", "caddy"])
def test_compose_service_hardening(svc):
    block = _compose_service(svc)
    assert re.search(r"^    build:", block, re.MULTILINE), f"{svc}: build context"
    assert "no-new-privileges:true" in block
    assert re.search(r"cap_drop:\s*\n\s*- ALL", block)
    assert re.search(r"^    read_only: true", block, re.MULTILINE)
    assert re.search(r"mem_limit:|memory:", block)
    assert re.search(r"pids_limit:|pids:", block)
    assert re.search(r"driver: json-file", block)
    assert re.search(r"max-size:", block) and re.search(r"max-file:", block)


def test_compose_production_env():
    api = _compose_service("api")
    for needle in ("VIDEO_FACTORY_ENV: production", "VF_HTTPS:",
                   "VIDEO_FACTORY_PUBLIC_BASE_URL:"):
        assert needle in api, needle
    web = _compose_service("web")
    for needle in ("ORIGIN:", "BODY_SIZE_LIMIT:", "VIDEO_FACTORY_API_URL: http://api:8000"):
        assert needle in web, needle
    assert "127.0.0.1" not in re.sub(r"#.*", "", api.split("command:")[-1].split("\n")[0])


def test_systemd_unit_is_type_simple():
    unit = (REPO_ROOT / "systemd" / "video-factory.service").read_text()
    assert re.search(r"^Type=simple$", unit, re.MULTILINE)
    assert not re.search(r"^Type=notify", unit, re.MULTILINE)


def test_no_reference_to_missing_rotate_script():
    for p in [*REPO_ROOT.glob("docs/*.md"), *REPO_ROOT.glob("systemd/*"),
              *SCRIPTS.glob("*.sh")]:
        text = p.read_text()
        for m in re.finditer(r"scripts/rotate_master_key\.sh", text):
            before = " ".join(text[max(0, m.start() - 40): m.start()].split())
            assert before.endswith(("There is no", "There is no `")), (
                f"{p}: points at scripts/rotate_master_key.sh, which does not "
                "exist (use the /api/v1/admin/keys/rotate endpoint)"
            )


# ────────────────────────────────────────────────────────────────────
# 5) Stripe price env names (A2 F-24)
# ────────────────────────────────────────────────────────────────────
@pytest.fixture
def billing(monkeypatch):
    from api import billing as mod
    for n in (*mod.STRIPE_PRICE_ENV.values(), *mod.LEGACY_STRIPE_PRICE_ENV.values()):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setattr(mod, "_warned_legacy_price_env", set())
    return mod


def test_canonical_price_env_wins(billing, monkeypatch):
    monkeypatch.setenv("STRIPE_PRO_PRICE_ID", "price_canonical")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_legacy")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert billing.configured_price_id("pro") == "price_canonical"


def test_legacy_price_env_still_works_with_warning(billing, monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_ENTERPRISE", "price_legacy_ent")
    with pytest.warns(DeprecationWarning, match="STRIPE_ENTERPRISE_PRICE_ID"):
        assert billing.configured_price_id("enterprise") == "price_legacy_ent"


def test_unset_price_env_is_empty(billing):
    assert billing.configured_price_id("pro") == ""
