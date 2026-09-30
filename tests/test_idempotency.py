"""Idempotency tests."""

from __future__ import annotations

from control_plane.idempotency import IdempotencyStore, ProviderSubmitReceipt, make_idem_key


def test_same_key_returns_same_receipt():
    store = IdempotencyStore()
    receipt = ProviderSubmitReceipt(provider_job_id="pj_1", accepted_metadata={})
    store.save("k1", receipt)
    got = store.lookup("k1")
    assert got is not None
    assert got.provider_job_id == "pj_1"


def test_different_keys_independent():
    store = IdempotencyStore()
    store.save("k1", ProviderSubmitReceipt(provider_job_id="pj_1", accepted_metadata={}))
    store.save("k2", ProviderSubmitReceipt(provider_job_id="pj_2", accepted_metadata={}))
    r1 = store.lookup("k1")
    r2 = store.lookup("k2")
    assert r1 is not None and r2 is not None
    assert r1.provider_job_id == "pj_1"
    assert r2.provider_job_id == "pj_2"


def test_save_does_not_overwrite_existing():
    store = IdempotencyStore()
    store.save("k", ProviderSubmitReceipt(provider_job_id="first", accepted_metadata={}))
    store.save("k", ProviderSubmitReceipt(provider_job_id="second", accepted_metadata={}))
    got = store.lookup("k")
    assert got is not None
    assert got.provider_job_id == "first"


def test_submit_once_replays_when_present():
    store = IdempotencyStore()
    r1 = ProviderSubmitReceipt(provider_job_id="pj_1", accepted_metadata={})
    out1 = store.submit_once("k", r1)
    r2 = ProviderSubmitReceipt(provider_job_id="pj_2", accepted_metadata={})
    out2 = store.submit_once("k", r2)
    assert out1.provider_job_id == out2.provider_job_id == "pj_1"


def test_make_idem_key_is_deterministic():
    k1 = make_idem_key("cmp_x", "vid_y", "mock", "hash_abc")
    k2 = make_idem_key("cmp_x", "vid_y", "mock", "hash_abc")
    assert k1 == k2
    k3 = make_idem_key("cmp_x", "vid_y", "mock", "hash_xyz")
    assert k1 != k3