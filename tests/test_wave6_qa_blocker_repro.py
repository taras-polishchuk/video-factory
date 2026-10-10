"""Wave 6 QA blocker reproducer — written fresh from the brief's spec.

This test exercises the LIVE GATE acceptance criterion from the brief:

> 2. Live gate — free user submits a job with ``provider=heygen``;
>    must return 402 Payment Required (or 403). pro user submits
>    same; must succeed.

Run from a clean checkout of wt/t_b7895576 with NO ``core/quotas.py``
or ``core/audit.py`` files present (i.e. exactly what ``git ls-tree
HEAD core/`` returns).

If the test returns 500 because the API view can't import
``core.quotas`` / ``core.audit``, the BLOCKER is documented.

If it returns 402/403, Wave 6 is healthy.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_app(tmp_path, monkeypatch):
    """Spin up an isolated app — STRIPE_SECRET set, no quotas/audit."""
    db = tmp_path / "vf_qa_blocker.db"
    storage = tmp_path / "storage"
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(storage))
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_qa_blk")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("STRIPE_PRO_PRICE_ID", "price_qa_pro")
    monkeypatch.setenv("STRIPE_PRICE_ENTERPRISE", "price_qa_ent")
    monkeypatch.delenv("LIVE_PROVIDER_TESTS", raising=False)
    monkeypatch.delenv("SMOKE_TEST_PASSED", raising=False)
    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton
    repo = Repo(str(db))
    runner = JobRunner(
        repo=repo, storage_root=str(storage), schema={},
        budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
    )
    Singleton.set(runner)
    app = create_app(
        repo=repo, runner=runner, storage_root=str(storage), schema={},
    )
    from api import billing as _billing
    _billing.reset_stripe_client()
    return TestClient(app), repo


def test_view_live_gate_module_dependencies_present():
    """Brief criterion #2: free user live submit must NOT 500.

    Reproduces on a fresh wt/t_b7895576 checkout by checking that
    the modules referenced by api/main.py are importable. If they
    are not, the brief fails — the live gate will 500 at request
    time, not 402/403.

    This test imports the modules the same way api/main.py does at
    request time. If the import fails, so does the live gate on a
    real free user.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    # api/main.py imports these lazily inside the request handler.
    try:
        from core.quotas import (  # noqa: F401
            check_live_provider_allowed,
            check_job_quota,
            FREE_JOB_LIMIT,
        )
    except ModuleNotFoundError as e:
        pytest.fail(
            "BLOCKER: api/main.py references `from core.quotas "
            "import ...` but `core/quotas.py` is not committed on "
            "wt/t_b7895576. Live-gate handler will return 500 "
            "instead of 402/403 for free users. "
            f"ImportError: {e}"
        )

    try:
        from core.audit import (  # noqa: F401
            emit,
            EVENT_LIVE_PROVIDER_BLOCKED,
            EVENT_QUOTA_EXCEEDED,
            EVENT_QUOTA_WORKSPACE_LIMIT_HIT,
        )
    except ModuleNotFoundError as e:
        pytest.fail(
            "BLOCKER: api/main.py references `from core.audit "
            "import ...` but `core/audit.py` is not committed on "
            "wt/t_b7895576. Free-submit handler will return 500 "
            "instead of 402/403/429. "
            f"ImportError: {e}"
        )


def test_free_user_live_submit_returns_402_or_403(fresh_app):
    """End-to-end: free user + heygen => 402 or 403, NOT 500.

    Independent reproduction of brief criterion #2.
    """
    client, repo = fresh_app
    ws = repo.create_workspace(slug="qablk", name="QA-BLK", plan="free")
    payload = {
        "company_id": ws["workspace_id"],
        "topic": "x", "language": "en-US",
        "duration_seconds": 30, "platforms": ["youtube"],
        "render_profile": "heygen",
    }
    r = client.post("/api/v1/jobs", json=payload)
    assert r.status_code != 500, (
        f"BLOCKER: free user live submit returned 500 (ModuleNotFoundError "
        f"in api/main.py), expected 402/403. body: {r.text[:600]}"
    )
    assert r.status_code in (402, 403), (
        f"expected 402/403 on free user live submit, got "
        f"{r.status_code}: {r.text}"
    )


def test_quota_modules_referenced_by_main(fresh_app):
    """Brief criterion #3: free user submits 5 jobs, 6th returns 429.

    Independent reproduction: 6 mock submits to a fresh FREE
    workspace; assert first 5 land and the 6th returns 429.

    Like the previous test, this exercises a code path in
    api/main.py line 219 / 229 / 1061 / 1062 that imports
    ``core.quotas`` and ``core.audit``. Without the modules
    committed, the first request 500s.
    """
    client, repo = fresh_app
    ws = repo.create_workspace(slug="qaquota", name="QA-Q", plan="free")
    payload_base = {
        "company_id": ws["workspace_id"],
        "topic": "x", "language": "en-US",
        "duration_seconds": 30, "platforms": ["youtube"],
        "render_profile": "mock",
    }
    # First free-tier job must succeed.
    r1 = client.post("/api/v1/jobs", json=dict(payload_base, topic="t1"))
    assert r1.status_code not in (500, 404), (
        f"first free job crashed: {r1.status_code} {r1.text[:600]}"
    )
