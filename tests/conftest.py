"""Pytest fixtures shared by the test suite.

Determinism contract (hardening card t_cfd27f30)
------------------------------------------------
The suite must be green serially, under ``pytest -n 6`` and in any
order. Three classes of cross-test leakage broke that before; the
autouse fixture ``_vf_test_isolation`` closes each one at the root so
an individual test cannot re-introduce it:

1. ``os.environ`` writes that were never undone (e.g. a leaked
   ``VIDEO_FACTORY_DB_KEY`` made every later plaintext ``Repo`` raise
   ``PlaintextDBRefused``). Anything a test changes in the
   environment is rolled back at teardown.
2. Background ``JobRunner`` pipeline threads outliving the test that
   started them (they kept writing into the finished test's tmp DB,
   competed for CPU and observed later tests' patches). Every
   ``job-*`` thread is joined at teardown.
3. Module globals patched by plain assignment instead of
   ``monkeypatch`` (``api.oidc._exchange_code``,
   ``api.oidc._provider_cache`` and its ``get``, ``shutil.which``,
   the ``JobRunner`` singleton, and the env-derived limits in
   ``auth.ratelimit``). They are restored at teardown.

Product env vars inherited from the caller's shell (``VIDEO_FACTORY_*``,
``VF_*``, ``STRIPE_*``, ...) are scrubbed once per process so a local
``export VIDEO_FACTORY_DB_KEY=...`` cannot change test outcomes.

Suite-guard meta tests
----------------------
Three historical tests (``*_still_green`` in waves 6, 6b and 7)
re-run the WHOLE suite in a subprocess. Inside a normal run that is
redundant (pytest already reports every test), it multiplies the run
time, and the nested runs used to recurse into each other. They are
marked ``@pytest.mark.suite_guard`` and are CI-only meta tests:
deselected by default (reported as "deselected", not skipped) and
enabled with ``--run-suite-guard`` or ``VF_RUN_SUITE_GUARD=1``. When
enabled, the child suite runs once per session (shared by all guard
tests via ``full_suite_run``) and never contains guard tests itself,
so there is no recursion.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Env prefixes the product reads. Scrubbed once per process so the
# caller's shell cannot leak into tests.
_SCRUB_PREFIXES = (
    "VIDEO_FACTORY_",
    "VF_",
    "STRIPE_",
    "SENTRY_",
    "HEYGEN_",
    "MAX_COST_PER_",
)
# Harness-level variables that are not product configuration.
_SCRUB_KEEP = {
    "VIDEO_FACTORY_REPO_ROOT",   # sys.path hint used by one test module
    "VF_RUN_SUITE_GUARD",        # opt-in for suite_guard tests
    "VF_SUITE_GUARD_CHILD",      # marks the child run of a suite guard
}

# Hang guard for draining JobRunner threads at teardown. Not a timing
# assumption: a mock job finishes in seconds. The bound only turns a
# genuine hang into a loud error instead of a stuck worker.
JOB_THREAD_DRAIN_TIMEOUT_S = 600.0

# Hang guard for the suite-guard child run.
SUITE_GUARD_TIMEOUT_S = 3600


# ── options / markers ─────────────────────────────────────────


def pytest_addoption(parser):
    parser.addoption(
        "--run-suite-guard",
        action="store_true",
        default=False,
        help=(
            "Run the CI-only suite_guard meta tests (they re-run the full "
            "suite in a subprocess). Also enabled by VF_RUN_SUITE_GUARD=1."
        ),
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "suite_guard: CI-only meta test that re-runs the whole suite in a "
        "subprocess. Deselected unless --run-suite-guard / "
        "VF_RUN_SUITE_GUARD=1.",
    )
    for key in list(os.environ):
        if key in _SCRUB_KEEP:
            continue
        if key.startswith(_SCRUB_PREFIXES):
            del os.environ[key]


def suite_guard_enabled(config) -> bool:
    if os.environ.get("VF_SUITE_GUARD_CHILD") == "1":
        # Never run guards inside a guard's child run (no recursion).
        return False
    return bool(
        config.getoption("--run-suite-guard")
        or os.environ.get("VF_RUN_SUITE_GUARD") == "1"
    )


def pytest_collection_modifyitems(config, items):
    if suite_guard_enabled(config):
        return
    keep, drop = [], []
    for item in items:
        (drop if item.get_closest_marker("suite_guard") else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


# ── per-test isolation ────────────────────────────────────────


_RATELIMIT_ENV = {
    "WINDOW_IP_SEC": ("VF_LOGIN_IP_WINDOW_SEC", str(15 * 60)),
    "WINDOW_EMAIL_SEC": ("VF_LOGIN_EMAIL_WINDOW_SEC", str(60 * 60)),
    "LIMIT_IP": ("VF_LOGIN_IP_LIMIT", "5"),
    "LIMIT_EMAIL": ("VF_LOGIN_EMAIL_LIMIT", "10"),
}

_ABSENT = object()


def _restore_environ(snapshot: dict) -> None:
    for key in list(os.environ):
        if key not in snapshot:
            del os.environ[key]
    for key, value in snapshot.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


def _sync_ratelimit_module() -> None:
    """``auth.ratelimit`` reads its limits from env at import time. If a
    test reloaded it under a custom env, reload it again so the next
    test sees limits derived from the restored env."""
    mod = sys.modules.get("auth.ratelimit")
    if mod is None:
        return
    for attr, (env_key, default) in _RATELIMIT_ENV.items():
        if getattr(mod, attr, None) != int(os.environ.get(env_key, default)):
            importlib.reload(mod)
            return


def _snapshot_module_attrs() -> list:
    """(owner, attr, value) triples to restore at teardown. ``value``
    is ``_ABSENT`` when the attribute lives only on the class (an
    instance-level override must then be deleted, not overwritten)."""
    saved = [(shutil, "which", shutil.which)]
    oidc = sys.modules.get("api.oidc")
    if oidc is not None:
        for attr in ("_exchange_code", "_provider_cache"):
            if hasattr(oidc, attr):
                saved.append((oidc, attr, getattr(oidc, attr)))
        cache = getattr(oidc, "_provider_cache", None)
        if cache is not None and hasattr(cache, "__dict__"):
            saved.append((cache, "get", cache.__dict__.get("get", _ABSENT)))
    runners = sys.modules.get("core.runners")
    if runners is not None:
        saved.append((runners.Singleton, "_instance", runners.Singleton._instance))
    return saved


def _restore_module_attrs(saved: list) -> None:
    for owner, attr, value in reversed(saved):
        if value is _ABSENT:
            owner.__dict__.pop(attr, None)
        elif getattr(owner, attr, _ABSENT) is not value:
            setattr(owner, attr, value)


def _drain_job_threads() -> list:
    """Join every live JobRunner pipeline thread (named ``job-<id>``).
    Returns the names of threads still alive after the hang guard."""
    me = threading.current_thread()
    stuck = []
    for t in threading.enumerate():
        if t is me or not t.name.startswith("job-"):
            continue
        t.join(JOB_THREAD_DRAIN_TIMEOUT_S)
        if t.is_alive():
            stuck.append(t.name)
    return stuck


@pytest.fixture(autouse=True)
def _vf_test_isolation():
    env_snapshot = dict(os.environ)
    attrs_snapshot = _snapshot_module_attrs()
    yield
    # Order matters: stop background jobs first (they may read env
    # and patched module globals), then roll the globals back.
    stuck = _drain_job_threads()
    _restore_environ(env_snapshot)
    _restore_module_attrs(attrs_snapshot)
    _sync_ratelimit_module()
    if stuck:
        pytest.fail(
            f"JobRunner thread(s) still running {JOB_THREAD_DRAIN_TIMEOUT_S:.0f}s "
            f"after the test finished: {stuck}"
        )


# ── suite guard support ───────────────────────────────────────


_SUMMARY_RE = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)"
)


@pytest.fixture(scope="session")
def full_suite_run():
    """Run the full suite once in a subprocess and return
    ``{"returncode", "stdout", "stderr", "counts"}``.

    Shared by every ``suite_guard`` test in the session. The child run
    never contains suite_guard tests (``VF_SUITE_GUARD_CHILD=1``), so
    it cannot recurse.
    """
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "VF_SUITE_GUARD_CHILD": "1"}
    env.pop("VF_RUN_SUITE_GUARD", None)
    # xdist exports PYTEST_XDIST_* to its workers; the child is a
    # plain serial run.
    for key in list(env):
        if key.startswith("PYTEST_XDIST") or key == "PYTEST_CURRENT_TEST":
            env.pop(key)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header",
         "-p", "no:cacheprovider", "--tb=line"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        timeout=SUITE_GUARD_TIMEOUT_S,
    )
    counts: dict = {}
    lines = proc.stdout.strip().splitlines()
    summary = lines[-1] if lines else ""
    for n, kind in _SUMMARY_RE.findall(summary):
        counts["error" if kind.startswith("error") else kind] = int(n)
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "summary": summary,
        "counts": counts,
    }


# ── generic fixtures ──────────────────────────────────────────


@pytest.fixture
def schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / "video-pack.schema.json"


@pytest.fixture
def schema(schema_path: Path) -> dict:
    return json.loads(schema_path.read_text())


@pytest.fixture
def output_dir(tmp_path) -> Path:
    return tmp_path / "artifacts"


def make_offline_pack_args():
    """Standard offline-demo arguments as plain dicts."""
    concept = {
        "title": "Why your coffee tastes better at altitude",
        "logline": "Short explainer about extraction and water boiling point.",
        "tone": "curious, calm, factual",
        "audience": "curious home baristas",
    }
    research = [
        {
            "text": "Water boils at lower temperatures at high altitude.",
            "source_title": "NIST Chemistry WebBook",
            "source_url": "https://webbook.nist.gov/chemistry/",
            "accessed_at": "2026-09-30T10:00:00Z",
            "confidence": "verified",
        }
    ]
    script = {
        "full_text": "Coffee tastes different up here. Water boils cooler.",
        "approved_by": "test-approver",
    }
    voice = {
        "provider": "mock",
        "voice_id": "mock_en_neutral",
        "ssml_or_text": script["full_text"],
    }
    captions = [
        {"start_seconds": 0.0, "end_seconds": 2.5, "text": "Coffee tastes different up here."},
        {"start_seconds": 2.5, "end_seconds": 6.0, "text": "Water boils cooler."},
    ]
    scenes = [
        {"scene_id": "sc_a", "order": 1, "target_duration_seconds": 4.0, "visual_intent": "Hook."},
        {"scene_id": "sc_b", "order": 2, "target_duration_seconds": 6.0, "visual_intent": "Body."},
    ]
    return concept, research, script, voice, captions, scenes
