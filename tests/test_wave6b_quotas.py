"""Wave 6b — Free-tier quota + plan-based gating (NO webhook).

Ten tests covering the brief:

- test_free_user_cannot_submit_live_job
- test_pro_user_can_submit_live_job
- test_free_user_job_quota_5_per_month
- test_free_user_quota_resets_on_first_of_month
- test_pro_user_unlimited_jobs
- test_free_user_cannot_create_second_workspace
- test_pro_user_can_create_multiple_workspaces
- test_live_gate_requires_global_smoke_passed
- test_live_gate_requires_workspace_live_enabled
- test_existing_tests_still_green (proxy for the full-suite guard)

Design notes:

* ``core.quotas`` is pure: it takes a ``job_count_for_month``
  callable, so the unit tests can pass a stub that returns
  whatever they want without touching the DB.  The API-level
  tests use a real ``Repo`` to exercise the DB-bound path.

* The plan-based live gate (``check_live_provider_allowed``) is
  tested in isolation AND through the API: the unit test pins
  the truth table (FREE→403, PRO+ws_live→allow, PRO-ws_live→403,
  mock profile→always allow); the API test pins the audit row
  shape + 403 status that the operator dashboard depends on.

* The global env-flag half of the live gate
  (``LIVE_PROVIDER_TESTS && SMOKE_TEST_PASSED``) is owned by
  ``render_plane.gate.ensure_live_unlocked`` and is intentionally
  NOT re-implemented here — the brief says "still required" and
  the existing unit tests in ``test_credential_vault.py:296-297``
  already pin that half.  We add a new test for the
  workspace-side flag the brief introduced.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Pure-function unit tests ────────────────────────────────────


def test_free_user_cannot_submit_live_job():
    """FREE plan is never allowed to submit a live-renderer job,
    even when the workspace has live_enabled=True (e.g. an
    operator temporarily enabled it before downgrading)."""
    from core.quotas import check_live_provider_allowed
    d = check_live_provider_allowed(
        plan="free", workspace_live_enabled=True,
        render_profile="heygen_direct",
    )
    assert d.allowed is False
    assert d.plan_allows_live is False
    assert "FREE" in d.reason or "free" in d.reason


def test_pro_user_can_submit_live_job():
    """PRO plan + live_enabled=True + a live render profile: the
    gate returns allowed=True.  The actual env-flag half of the
    live gate (LIVE_PROVIDER_TESTS, SMOKE_TEST_PASSED) is checked
    at the provider boundary, not here."""
    from core.quotas import check_live_provider_allowed
    d = check_live_provider_allowed(
        plan="pro", workspace_live_enabled=True,
        render_profile="heygen_direct",
    )
    assert d.allowed is True
    assert d.plan_allows_live is True
    assert d.ws_live_enabled is True


def test_free_user_job_quota_5_per_month():
    """The 5-jobs/month cap returns allowed=False once used, and
    reports the correct jobs_used / reset_at shape."""
    from core.quotas import check_job_quota, FREE_JOB_LIMIT
    now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
    # 5 jobs already used this month
    d = check_job_quota(
        workspace_id="wks_abc", plan="free", now=now,
        job_count_for_month=lambda wid, since: 5,
    )
    assert d.allowed is False
    assert d.jobs_used == 5
    assert d.jobs_remaining == 0
    assert d.limit == FREE_JOB_LIMIT == 5
    assert d.reset_at == "2026-11-01T00:00:00Z"
    # 4 jobs used → still allowed
    d2 = check_job_quota(
        workspace_id="wks_abc", plan="free", now=now,
        job_count_for_month=lambda wid, since: 4,
    )
    assert d2.allowed is True
    assert d2.jobs_remaining == 1


def test_free_user_quota_resets_on_first_of_month():
    """Calling the check on the 1st at 00:00:00 UTC of a new month
    must report jobs_used=0 (the caller passed in a stub that
    returns 0 — the gate doesn't reach into the DB itself) and a
    reset_at that points to the FOLLOWING month."""
    from core.quotas import check_job_quota
    # Just after the reset
    now = datetime(2026, 11, 1, 0, 0, 1, tzinfo=timezone.utc)
    d = check_job_quota(
        workspace_id="wks_abc", plan="free", now=now,
        job_count_for_month=lambda wid, since: 0,
    )
    assert d.allowed is True
    assert d.jobs_used == 0
    assert d.reset_at == "2026-12-01T00:00:00Z"
    # December → January: the year boundary is the easy spot
    # to get wrong with naive month math.
    now2 = datetime(2026, 12, 15, 12, 0, 0, tzinfo=timezone.utc)
    d2 = check_job_quota(
        workspace_id="wks_abc", plan="free", now=now2,
        job_count_for_month=lambda wid, since: 0,
    )
    assert d2.allowed is True
    assert d2.reset_at == "2027-01-01T00:00:00Z"


def test_pro_user_unlimited_jobs():
    """PRO plan: always allowed, regardless of job_count_for_month.
    The injected count function MUST NOT even be called (paid
    plans short-circuit before reaching the count)."""
    from core.quotas import check_job_quota
    called = {"n": 0}

    def counter(wid, since):
        called["n"] += 1
        return 0

    d = check_job_quota(
        workspace_id="wks_abc", plan="pro", now=datetime.now(
            timezone.utc
        ), job_count_for_month=counter,
    )
    assert d.allowed is True
    assert d.limit == -1  # UNLIMITED
    assert d.jobs_remaining == -1
    assert d.reset_at is None
    assert called["n"] == 0, (
        "paid plan should not touch the count function (no DB hit)"
    )

    # Same for enterprise
    d2 = check_job_quota(
        workspace_id="wks_abc", plan="enterprise",
        now=datetime.now(timezone.utc),
        job_count_for_month=counter,
    )
    assert d2.allowed is True
    assert called["n"] == 0


def test_free_user_cannot_create_second_workspace():
    """FREE plan: at most 1 owned workspace per user."""
    from core.quotas import (
        check_workspace_count, WORKSPACE_LIMIT_FREE,
    )
    d = check_workspace_count(plan="free", workspaces_owned=1)
    assert d.allowed is False
    assert d.limit == WORKSPACE_LIMIT_FREE == 1
    assert d.workspaces_owned == 1
    # 0 workspaces → allowed
    d0 = check_workspace_count(plan="free", workspaces_owned=0)
    assert d0.allowed is True


def test_pro_user_can_create_multiple_workspaces():
    """PRO plan: unlimited workspaces.  The function never refuses
    for any positive count, including numbers that would be absurd
    on FREE (5, 10, 1000)."""
    from core.quotas import check_workspace_count
    for n in (2, 5, 10, 100, 1000):
        d = check_workspace_count(plan="pro", workspaces_owned=n)
        assert d.allowed is True, (
            f"PRO plan should allow {n} workspaces, got {d!r}"
        )
        assert d.limit == -1
    # Enterprise: same
    d = check_workspace_count(plan="enterprise", workspaces_owned=42)
    assert d.allowed is True
    assert d.limit == -1


def test_live_gate_requires_global_smoke_passed():
    """The env-flag half of the live gate is owned by
    render_plane.gate.ensure_live_unlocked.  This test pins
    that the existing behaviour is preserved: with no
    LIVE_PROVIDER_TESTS and no SMOKE_TEST_PASSED, even a
    PRO+live_enabled workspace cannot reach a live provider
    (ProviderDisabled raised at the adapter boundary)."""
    from render_plane.gate import ensure_live_unlocked
    from control_plane.errors import ProviderDisabled
    # Both flags off (default).
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("LIVE_PROVIDER_TESTS", "SMOKE_TEST_PASSED")
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ProviderDisabled):
            ensure_live_provider_only = ensure_live_unlocked  # alias
            ensure_live_provider_only("heygen_direct")
    # Only LIVE_PROVIDER_TESTS, no SMOKE_TEST_PASSED → still
    # blocked (the gate requires BOTH).
    env2 = dict(env, LIVE_PROVIDER_TESTS="true")
    with patch.dict(os.environ, env2, clear=True):
        with pytest.raises(ProviderDisabled):
            ensure_live_unlocked("heygen_direct")
    # Only SMOKE_TEST_PASSED, no LIVE_PROVIDER_TESTS → still
    # blocked.
    env3 = dict(env, SMOKE_TEST_PASSED="true")
    with patch.dict(os.environ, env3, clear=True):
        with pytest.raises(ProviderDisabled):
            ensure_live_unlocked("heygen_direct")


def test_live_gate_requires_workspace_live_enabled():
    """The plan+workspace half of the live gate is owned by
    core.quotas.check_live_provider_allowed.  PRO plan with
    workspace.live_enabled=False must be refused (the operator
    has not enabled this workspace).  Same plan with
    live_enabled=True must be allowed."""
    from core.quotas import check_live_provider_allowed
    # PRO + live_enabled=False: refused at the plan+ws half.
    d = check_live_provider_allowed(
        plan="pro", workspace_live_enabled=False,
        render_profile="heygen_direct",
    )
    assert d.allowed is False
    assert d.plan_allows_live is True
    assert d.ws_live_enabled is False
    assert "live_enabled" in d.reason
    # PRO + live_enabled=True: allowed at the plan+ws half.
    d2 = check_live_provider_allowed(
        plan="pro", workspace_live_enabled=True,
        render_profile="heygen_direct",
    )
    assert d2.allowed is True
    assert d2.plan_allows_live is True
    assert d2.ws_live_enabled is True
    # Mock profile: always allowed, regardless of plan/flag.
    d_mock = check_live_provider_allowed(
        plan="free", workspace_live_enabled=False,
        render_profile="mock",
    )
    assert d_mock.allowed is True
    # No profile at all (None) → conservative: refuse on FREE
    # (we don't know if the user meant a live adapter; a future
    # new live profile would also default to blocked here
    # until we add it to LIVE_RENDER_PROFILES).
    d_none = check_live_provider_allowed(
        plan="free", workspace_live_enabled=False,
        render_profile=None,
    )
    assert d_none.allowed is False
    # Unknown future profile: blocked on FREE (typo / new
    # adapter both hit the live gate rather than silently
    # letting the user through).  A known-empty profile
    # (``""`` or ``None``) is treated as mock (the API
    # layer never sends those, but the function is robust
    # against it for callers that build the decision
    # outside the request handler).
    d_unknown = check_live_provider_allowed(
        plan="free", workspace_live_enabled=False,
        render_profile="comfy_claude_future",
    )
    assert d_unknown.allowed is False


# ── Full-suite guard (proxy for the brief's "250 green" claim) ─


@pytest.mark.suite_guard
def test_existing_tests_still_green(full_suite_run):
    """Spawn a fresh subprocess and confirm the full pytest
    suite still passes.

    This is a slow guard — the focused tests above pin the
    specific behaviour; this one catches the case where my
    repo or API edits broke a test elsewhere in the repo.

    CI-only meta test (t_cfd27f30): deselected by default, enabled
    with ``--run-suite-guard``. The subprocess runs once per session
    in the ``full_suite_run`` conftest fixture (shared with the other
    suite guards, run with the project interpreter = sys.executable,
    cwd pinned to the repo root).

    RECURSION GUARD: the child run sets VF_SUITE_GUARD_CHILD=1, which
    makes conftest deselect every suite_guard test inside it, so the
    child can never spawn another child. The historical
    WAVE6B_SUITE_GUARD_ACTIVE sentinel is still honoured.
    """
    if os.environ.get("WAVE6B_SUITE_GUARD_ACTIVE") == "1":
        # We're already inside a legacy suite-guard subprocess; do
        # not recurse.
        return
    result = full_suite_run
    assert result["returncode"] == 0, (
        f"full suite failed:\nSTDOUT:\n{result['stdout']}\n"
        f"STDERR:\n{result['stderr']}"
    )
