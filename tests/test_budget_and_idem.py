"""Tests for core/budget_enforcer and core/idempotency durability."""
from __future__ import annotations

import os
import pathlib

import pytest

from control_plane.errors import BudgetExceeded
from core.budget_enforcer import BudgetEnforcer, preflight_or_block
from control_plane.idempotency import (
    DurableIdempotencyStore, IdempotencyStore, ProviderSubmitReceipt,
)


def test_unknown_cost_refused():
    b = BudgetEnforcer(max_cost_per_video_usd=5.0, max_cost_per_batch_usd=20.0)
    d = b.preflight(estimated_cost_usd=None)
    assert d.allowed is False
    assert "unknown cost" in d.reason


def test_known_cost_under_ceiling_allowed():
    b = BudgetEnforcer(max_cost_per_video_usd=5.0, max_cost_per_batch_usd=20.0)
    d = b.preflight(estimated_cost_usd=3.0)
    assert d.allowed is True


def test_over_video_ceiling_refused():
    b = BudgetEnforcer(max_cost_per_video_usd=5.0, max_cost_per_batch_usd=20.0)
    d = b.preflight(estimated_cost_usd=10.0)
    assert d.allowed is False
    assert "per-video" in d.reason


def test_batch_ceiling_tracked():
    b = BudgetEnforcer(max_cost_per_video_usd=100.0, max_cost_per_batch_usd=10.0)
    b.record_spend(7.0)
    d = b.preflight(estimated_cost_usd=5.0)  # 7+5 > 10
    assert d.allowed is False
    assert "batch" in d.reason


def test_preflight_or_block_raises():
    b = BudgetEnforcer(max_cost_per_video_usd=5.0, max_cost_per_batch_usd=20.0)
    with pytest.raises(BudgetExceeded):
        preflight_or_block(b, estimated_cost_usd=None)


def test_durable_idempotency_persists(tmp_path):
    db = tmp_path / "idem.db"
    s1 = DurableIdempotencyStore(str(db))
    s1.save("k1", ProviderSubmitReceipt(provider_job_id="p1", accepted_metadata={"a": 1}))
    # New instance — proves restart-survival.
    s2 = DurableIdempotencyStore(str(db))
    r = s2.lookup("k1")
    assert r is not None
    assert r.provider_job_id == "p1"
    assert r.accepted_metadata == {"a": 1}


def test_durable_idempotency_submit_once_idempotent(tmp_path):
    db = tmp_path / "idem.db"
    s = DurableIdempotencyStore(str(db))
    rec = ProviderSubmitReceipt(provider_job_id="p2", accepted_metadata={})
    r1 = s.submit_once("k2", rec)
    r2 = s.submit_once("k2", rec)
    assert r1.provider_job_id == r2.provider_job_id


def test_in_memory_idempotency_unaffected_by_durable():
    """Existing IdempotencyStore still works."""
    s = IdempotencyStore()
    s.save("k", ProviderSubmitReceipt(provider_job_id="p", accepted_metadata={}))
    assert s.lookup("k").provider_job_id == "p"