"""UI U1 — live browser walk of the customer path (real Chromium).

Boots the real FastAPI app (temp DB + storage, mock mode) and a real
`vite dev` SvelteKit server on free ports, then drives them with the
Playwright library already declared in web/package.json
(web/e2e/customer-path.mjs). It reproduces, end to end:

  signup -> overview + onboarding -> "Add provider key" -> credentials
  page renders -> every route renders with no key -> labelled controls
  -> bible save + approve (needs the CSRF token) -> save a key ->
  logout -> login through the FORM with ?next= (A-2) -> wrong password
  inline error -> open-redirect probes -> 390px menu navigation (A-6)

Skipped (not failed) only when the toolchain is absent: no node, no
node_modules, or no Chromium for Playwright.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
WALKER = WEB_ROOT / "e2e" / "customer-path.mjs"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _toolchain_or_skip() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    if not (WEB_ROOT / "node_modules" / ".bin" / "vite").exists():
        pytest.skip("vite not installed (no web/node_modules)")
    probe = subprocess.run(
        [node, "-e",
         "import('playwright').then(m => process.stdout.write(m.chromium.executablePath()))"
         ".catch(() => process.exit(3))"],
        cwd=str(WEB_ROOT), capture_output=True, text=True, timeout=30,
    )
    if probe.returncode != 0 or not probe.stdout or not pathlib.Path(probe.stdout).exists():
        pytest.skip("playwright chromium not installed")
    return node


@pytest.fixture()
def live_stack(tmp_path):
    node = _toolchain_or_skip()
    api_port, web_port = _free_port(), _free_port()
    session_secret = "s" * 64
    env = os.environ.copy()
    env.update({
        "VIDEO_FACTORY_SESSION_SECRET": session_secret,
        "VIDEO_FACTORY_SECRET_KEY": Fernet.generate_key().decode(),
        "VIDEO_FACTORY_DB": str(tmp_path / "vf.db"),
        "VIDEO_FACTORY_STORAGE": str(tmp_path / "storage"),
        "VIDEO_FACTORY_ENV": "dev",
        "VF_LOGIN_IP_LIMIT": "1000",
        "VF_LOGIN_EMAIL_LIMIT": "1000",
        "PYTHONPATH": str(REPO_ROOT),
    })
    env.pop("VIDEO_FACTORY_DB_KEY", None)
    # Logs go to files, not pipes: an undrained pipe fills at ~64 KB of
    # uvicorn access logs and blocks the server.
    api_log = open(tmp_path / "api.log", "w+")
    web_log = open(tmp_path / "web.log", "w+")

    def _tail(f) -> str:
        f.flush()
        f.seek(0)
        return f.read()[-2000:]

    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(api_port)],
        cwd=str(REPO_ROOT), env=env, stdout=api_log, stderr=subprocess.STDOUT,
    )
    web_env = dict(env)
    web_env.update({
        "VIDEO_FACTORY_API_URL": f"http://127.0.0.1:{api_port}",
        "VITE_API_TARGET": f"http://127.0.0.1:{api_port}",
    })
    web = subprocess.Popen(
        [node, str(WEB_ROOT / "node_modules" / "vite" / "bin" / "vite.js"), "dev",
         "--port", str(web_port), "--strictPort", "--host", "127.0.0.1"],
        cwd=str(WEB_ROOT), env=web_env, stdout=web_log, stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_http(f"http://127.0.0.1:{api_port}/healthz", 40):
            pytest.fail("API did not come up:\n" + _tail(api_log))
        if not _wait_http(f"http://127.0.0.1:{web_port}/login", 90):
            pytest.fail("vite dev did not come up:\n" + _tail(web_log))
        yield node, f"http://127.0.0.1:{web_port}"
    finally:
        for proc in (web, api):
            proc.terminate()
        for proc in (web, api):
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        api_log.close()
        web_log.close()


def test_customer_path_in_a_real_browser(live_stack, tmp_path):
    node, base = live_stack
    out = tmp_path / "walk.json"
    proc = subprocess.run(
        [node, str(WALKER), base, str(out)],
        cwd=str(WEB_ROOT), capture_output=True, text=True, timeout=240,
    )
    assert out.exists(), f"walker produced no result\n{proc.stdout[-800:]}\n{proc.stderr[-1500:]}"
    steps = json.loads(out.read_text())["steps"]
    assert steps, "walker recorded no steps"
    for s in steps:  # visible with `pytest -s` / on failure
        print(f"[{'ok' if s['ok'] else 'FAIL'}] {s['name']} :: {s['note']}")
    failed = [f"{s['name']} :: {s['note']}" for s in steps if not s["ok"]]
    assert not failed, "browser walk failed:\n  " + "\n  ".join(failed)
    assert len(steps) >= 15
