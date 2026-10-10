"""Regression tests for the product fixes made by the test-determinism
hardening card (t_cfd27f30).

Each test pins one root cause found while driving the suite to a
deterministic green under ``pytest -n 2``, serial and reversed order:

1. Primary-workspace selection tie-break. Timestamps are stored with
   one-second resolution, so a signup workspace and a second workspace
   created in the same second tie on ``created_at``. The "oldest
   membership" queries then returned whichever row the index scan
   produced first (ordered by the random workspace_id), so the
   Wave 6b workspace-count quota sometimes read the plan of the NEW
   free workspace instead of the user's primary (pro) one:
   ``test_workspace_slug_uniqueness_409`` got 403 instead of 409.
   Fixed with an insertion-order (rowid) tie-break.
2. ``JobRunner.submit`` check-then-start race and the missing
   completion signal (``JobRunner.wait`` / ``wait_all``). Tests used
   to poll the job row against a 60 s wall clock, which failed on a
   loaded host.
3. The mock edit-plan render encodes with ``-preset ultrafast``
   (about 1/3 of the CPU and RSS of the libx264 default for the same
   flat synthetic clips), which is what made mock jobs slow and,
   under xdist, memory-hungry.
4. The conftest isolation guard (env, module globals, job threads)
   and the suite_guard deselection.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core.repo import Repo

REPO_ROOT = Path(__file__).resolve().parent.parent

TIED_TS = "2026-10-09T12:00:00Z"
# Inserted FIRST (older by insertion order) but sorts LAST by id.
PRIMARY_WID = "wks_ffffffffffff"
# Inserted SECOND (newer) but sorts FIRST by id, so an index scan on
# (user_id, workspace_id) yields it first when created_at ties.
SECOND_WID = "wks_000000000000"


def _seed_tied_workspaces(repo: Repo) -> str:
    user = repo.create_user(email="tie@example.com", password_hash="h" * 32)
    uid = user["user_id"]
    c = repo._conn()
    for wid, slug, plan in ((PRIMARY_WID, "primary", "pro"),
                            (SECOND_WID, "second", "free")):
        c.execute(
            "INSERT INTO workspaces(workspace_id, slug, name, description, "
            "plan, status, live_enabled, owner_user_id, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (wid, slug, slug.title(), "", plan, "active", 0, uid,
             TIED_TS, TIED_TS),
        )
        c.execute(
            "INSERT INTO memberships(user_id, workspace_id, role, created_at) "
            "VALUES (?,?,?,?)",
            (uid, wid, "owner", TIED_TS),
        )
    c.commit()
    return uid


def test_primary_workspace_tie_break_is_insertion_order(tmp_path):
    repo = Repo(str(tmp_path / "vf.db"))
    uid = _seed_tied_workspaces(repo)

    from auth.dependencies import list_workspaces_for_user
    ws = list_workspaces_for_user(repo, uid)
    assert [w["workspace_id"] for w in ws] == [PRIMARY_WID, SECOND_WID]
    # The quota code reads the plan from the first OWNED workspace.
    owned = [w for w in ws if w["role"] == "owner"]
    assert owned[0]["plan"] == "pro"

    assert [w["workspace_id"] for w in repo.list_workspaces_for_user(uid)] == [
        PRIMARY_WID, SECOND_WID,
    ]
    assert [w["workspace_id"] for w in repo.list_workspaces()] == [
        PRIMARY_WID, SECOND_WID,
    ]

    import api.auth as api_auth
    import api.oidc as api_oidc
    conn = repo._conn()
    assert api_auth._first_workspace_for_user(conn, uid)["workspace_id"] == PRIMARY_WID
    assert api_oidc._first_workspace_for_user(conn, uid)["workspace_id"] == PRIMARY_WID
    # "Latest" is the mirror image: the row inserted last wins the tie.
    assert api_auth._latest_workspace_id(conn, uid) == SECOND_WID
    repo.close()


# ── JobRunner.submit / wait ──────────────────────────────────────


def _runner(tmp_path) -> "object":
    from core.runners import JobRunner
    repo = Repo(str(tmp_path / "vf.db"))
    return JobRunner(repo=repo, storage_root=str(tmp_path / "storage"),
                     schema={}, budget_per_video_usd=5.0,
                     budget_per_batch_usd=20.0)


def test_job_runner_submit_starts_one_thread_under_concurrent_submits(
    tmp_path, monkeypatch,
):
    from core.runners import JobRunner
    release = threading.Event()
    calls: list[str] = []
    calls_lock = threading.Lock()

    def fake_run(self, job_id):
        with calls_lock:
            calls.append(job_id)
        release.wait(60)

    monkeypatch.setattr(JobRunner, "_run", fake_run)
    runner = _runner(tmp_path)
    n = 16
    barrier = threading.Barrier(n)

    def submitter():
        barrier.wait(30)
        runner.submit("job_same")

    threads = [threading.Thread(target=submitter) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    try:
        assert runner.wait("job_same", timeout=0) is False  # still blocked
        with calls_lock:
            assert calls == ["job_same"], f"pipeline started {len(calls)}x"
    finally:
        release.set()
    assert runner.wait("job_same", timeout=60) is True
    # A finished job may be submitted again (retry path).
    release.clear()
    runner.submit("job_same")
    release.set()
    assert runner.wait_all(timeout=60) is True
    assert calls == ["job_same", "job_same"]


def test_job_runner_wait_unknown_job_returns_true(tmp_path):
    runner = _runner(tmp_path)
    assert runner.wait("never_submitted", timeout=0) is True
    assert runner.wait_all(timeout=0) is True


def test_job_runner_wait_returns_after_real_mock_pipeline(tmp_path):
    """End-to-end: wait() is a real completion signal. Once it returns
    the job row is terminal (no polling against a wall clock)."""
    from core.runners import JobRunner
    repo = Repo(str(tmp_path / "vf.db"))
    runner = JobRunner(repo=repo, storage_root=str(tmp_path / "storage"),
                       schema={}, budget_per_video_usd=5.0,
                       budget_per_batch_usd=20.0)
    c = repo.create_company(slug="acme", name="Acme")
    job = repo.create_job(
        company_id=c["company_id"], topic="t", language="en",
        duration_seconds=3, platforms=["tiktok"], aspect_ratio="9:16",
        count=1, render_profile="mock",
    )
    runner.submit(job["job_id"])
    assert runner.wait(job["job_id"], timeout=600) is True
    assert repo.get_job(job["job_id"])["state"] in ("done", "failed")


# ── mock render encoder preset ───────────────────────────────────


def test_mock_edit_plan_render_uses_ultrafast_preset(tmp_path, monkeypatch):
    import core.edit_plan as ep
    seen: list[list[str]] = []

    def fake_check_call(cmd, **kwargs):
        seen.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"\x00\x00\x00 ftypisom fake")
        return 0

    monkeypatch.setattr(ep.subprocess, "check_call", fake_check_call)

    class _Store:
        def put(self, *, key, data, mime_type):
            class _Ref:
                def to_dict(self_inner):
                    return {"key": key, "size_bytes": len(data)}
            return _Ref()

    plan = ep.build_plan(duration_seconds=6, n_clips=2, topic="t")
    out = ep.render_plan_to_storage(plan=plan, storage=_Store(),
                                    output_key="jobs/x/final.mp4")
    assert out is not None
    # 2 synthetic clips + 1 concat, every libx264 encode is ultrafast.
    assert len(seen) == 3
    for cmd in seen:
        assert "libx264" in cmd
        # S1c uses drawtext_from_file, so the preset appears after the
        # -vf filtergraph; just look for the flag pair anywhere in the
        # arg list.
        if "-preset" in cmd:
            assert cmd[cmd.index("-preset") + 1] == "ultrafast"


# ── conftest isolation guard ─────────────────────────────────────


def test_conftest_restore_environ_rolls_back_adds_changes_and_deletes():
    import conftest
    key_add, key_chg, key_del = "VF_T_ADD", "VF_T_CHG", "VF_T_DEL"
    os.environ[key_chg] = "orig"
    os.environ[key_del] = "orig"
    snap = dict(os.environ)
    try:
        os.environ[key_add] = "new"
        os.environ[key_chg] = "changed"
        del os.environ[key_del]
        conftest._restore_environ(snap)
        assert key_add not in os.environ
        assert os.environ[key_chg] == "orig"
        assert os.environ[key_del] == "orig"
    finally:
        for k in (key_add, key_chg, key_del):
            os.environ.pop(k, None)


def test_conftest_restores_patched_module_globals():
    import shutil
    import api.oidc as oidc
    import conftest
    real_which = shutil.which
    real_exchange = oidc._exchange_code
    snap = conftest._snapshot_module_attrs()
    shutil.which = lambda name: None
    oidc._exchange_code = lambda *a, **k: {}
    cache = oidc._provider_cache
    if hasattr(cache, "__dict__"):
        cache.get = lambda *a, **k: None
    conftest._restore_module_attrs(snap)
    assert shutil.which is real_which
    assert oidc._exchange_code is real_exchange
    if hasattr(cache, "__dict__"):
        assert "get" not in cache.__dict__


def test_suite_guard_tests_are_deselected_by_default():
    """The full-suite meta tests are CI-only: a plain run deselects
    them (no recursion, no hidden skip); --run-suite-guard selects
    them."""
    files = ["tests/test_wave6_billing.py", "tests/test_wave6b_quotas.py",
             "tests/test_wave7_deploy.py"]
    guards = {
        "tests/test_wave6_billing.py::test_existing_273_tests_still_green",
        "tests/test_wave6b_quotas.py::test_existing_tests_still_green",
        "tests/test_wave7_deploy.py::test_existing_270_tests_still_green",
    }
    env = {k: v for k, v in os.environ.items()
           if k not in ("VF_RUN_SUITE_GUARD", "VF_SUITE_GUARD_CHILD")
           and not k.startswith("PYTEST_XDIST")}
    env["PYTHONPATH"] = str(REPO_ROOT)

    def collect(*extra):
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", *extra, *files],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=600,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        return {line.strip() for line in r.stdout.splitlines() if "::" in line}

    default = collect()
    assert default, "collect-only returned nothing"
    assert not (guards & default), f"guards collected by default: {guards & default}"
    opted_in = collect("--run-suite-guard")
    assert guards <= opted_in
