"""Wave 6 re-verify (t_f22629ed) -- adversarial probes beyond the ported QA suites.

Everything here runs offline: FastAPI TestClient + payloads signed with the real
Stripe SDK signature helper (no network, no live Stripe).

Probes target the *class* of defect behind F-W6C-001 (webhook silently demotes a
higher plan) and the idempotency contract ("record before process") that the
earlier QA suites did not exercise on the failure path.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Tuple

import pytest
from fastapi.testclient import TestClient

WEBHOOK_PATH = "/api/v1/webhooks/stripe"
TEST_SECRET = "whsec_reverify_probe"
ENT_PRICE = "price_reverify_enterprise"
PRO_PRICE = "price_reverify_pro"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "vf_reverify.db"
    storage = tmp_path / "storage"
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(storage))
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("STRIPE_PRICE_ENTERPRISE", ENT_PRICE)
    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton

    repo = Repo(str(db))
    runner = JobRunner(
        repo=repo, storage_root=str(storage), schema={},
        budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
    )
    Singleton.set(runner)
    app = create_app(repo=repo, runner=runner, storage_root=str(storage), schema={})
    return TestClient(app), repo


def _sign(payload: Dict[str, Any]) -> Tuple[bytes, str]:
    import stripe._webhook as _wh

    body_str = json.dumps(payload, separators=(",", ":"))
    header = _wh.WebhookSignature.generate_signature_header(body_str, TEST_SECRET)
    return body_str.encode(), header


def _post(client, payload):
    body, header = _sign(payload)
    return client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )


def _ws(repo, plan="free", cus=None, sub=None) -> str:
    ws = repo.create_workspace(
        slug=f"rv-{int(time.time() * 1_000_000)}", name="RV",
        owner_user_id=None, plan="free",
    )
    wid = ws["workspace_id"]
    if plan != "free" or cus:
        repo.update_workspace_billing(
            wid, plan=plan, stripe_customer_id=cus, stripe_subscription_id=sub,
            current_period_end=2000000000,
        )
    return wid


def _sub_event(eid, cus, sub, price, status="active"):
    return {
        "id": eid, "type": "customer.subscription.updated",
        "data": {"object": {
            "id": sub, "customer": cus, "status": status,
            "current_period_end": 2100000000,
            "items": {"data": [{"id": "si_1", "price": {"id": price}}]},
        }},
    }


def _checkout_event(eid, wid, cus, sub):
    return {
        "id": eid, "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_1", "client_reference_id": wid,
            "customer": cus, "subscription": sub,
        }},
    }


# --- upgrade path -----------------------------------------------------------

def test_upgrade_path_free_to_pro_to_enterprise(env):
    client, repo = env
    wid = _ws(repo)
    r = _post(client, _checkout_event("evt_rv_up1", wid, "cus_rv_up", "sub_rv_up"))
    assert r.status_code == 200, r.text
    assert repo.get_workspace(wid)["plan"] == "pro"
    r = _post(client, _sub_event("evt_rv_up2", "cus_rv_up", "sub_rv_up", ENT_PRICE))
    assert r.status_code == 200, r.text
    assert repo.get_workspace(wid)["plan"] == "enterprise"


# --- F-W6C-001 class: webhook must never silently demote -------------------

def test_stale_checkout_completed_does_not_demote_enterprise(env):
    """A replayed / late checkout.session.completed (fresh event id) for a
    workspace that is already enterprise must not rewrite plan to pro."""
    client, repo = env
    wid = _ws(repo, plan="enterprise", cus="cus_rv_ent", sub="sub_rv_ent")
    r = _post(client, _checkout_event("evt_rv_stale_co", wid, "cus_rv_ent", "sub_rv_ent"))
    assert r.status_code == 200, r.text
    assert repo.get_workspace(wid)["plan"] == "enterprise", (
        "checkout.session.completed silently demoted enterprise -> pro "
        "(PLAN_RANK guard only covers customer.subscription.updated)"
    )


def test_subscription_updated_downgrade_is_refused_and_audited(env):
    client, repo = env
    wid = _ws(repo, plan="enterprise", cus="cus_rv_dg", sub="sub_rv_dg")
    r = _post(client, _sub_event("evt_rv_dg", "cus_rv_dg", "sub_rv_dg", PRO_PRICE))
    assert r.status_code == 200, r.text
    assert r.json().get("downgrade_refused") is True
    assert repo.get_workspace(wid)["plan"] == "enterprise"
    rows = repo._conn().execute(
        "SELECT COUNT(*) FROM audit_log WHERE event=?",
        ("billing.subscription_downgrade_blocked",),
    ).fetchone()[0]
    assert rows == 1


# --- idempotency failure path ----------------------------------------------

def test_failed_processing_is_retried_not_swallowed_as_duplicate(env, monkeypatch):
    """If applying the event raises (-> 5xx), Stripe retries the SAME event id.
    The retry must be processed, not acked as `duplicate` and lost forever."""
    client, repo = env
    wid = _ws(repo)
    payload = _checkout_event("evt_rv_retry", wid, "cus_rv_retry", "sub_rv_retry")

    real = repo.update_workspace_billing
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db failure")
        return real(*a, **kw)

    monkeypatch.setattr(repo, "update_workspace_billing", flaky)
    c2 = TestClient(client.app, raise_server_exceptions=False)
    r1 = _post(c2, payload)
    assert r1.status_code >= 500, r1.text
    assert repo.get_workspace(wid)["plan"] == "free"

    r2 = _post(c2, payload)  # Stripe retry, same event id
    assert r2.status_code == 200, r2.text
    assert repo.get_workspace(wid)["plan"] == "pro", (
        "retry of an event whose first attempt failed was acked as duplicate "
        f"and never applied: {r2.json()}"
    )


def test_checkout_for_unknown_workspace_is_acked_not_retried_forever(env):
    """Permanent error (workspace id does not exist) must be 2xx-ignored,
    not a 5xx that Stripe retries for days."""
    client, repo = env
    r = _post(client, _checkout_event("evt_rv_nows", "ws_does_not_exist",
                                      "cus_rv_nows", "sub_rv_nows"))
    assert r.status_code == 200, r.text
    assert r.json().get("ignored") is True


def test_released_event_is_reprocessed_but_processed_event_stays_deduped(env):
    client, repo = env
    wid = _ws(repo)
    payload = _checkout_event("evt_rv_dedupe", wid, "cus_rv_dd", "sub_rv_dd")
    r1 = _post(client, payload)
    assert r1.status_code == 200 and r1.json().get("ok") is True
    # processed event: release_stripe_event must refuse to re-open it
    assert repo.release_stripe_event("evt_rv_dedupe") is False
    r2 = _post(client, payload)
    assert r2.json().get("duplicate") is True


def test_checkout_downgrade_is_audited(env):
    client, repo = env
    wid = _ws(repo, plan="enterprise", cus="cus_rv_ca", sub="sub_rv_ca")
    r = _post(client, _checkout_event("evt_rv_ca", wid, "cus_rv_ca", "sub_rv_ca"))
    assert r.status_code == 200 and r.json().get("downgrade_refused") is True
    n = repo._conn().execute(
        "SELECT COUNT(*) FROM audit_log WHERE event=?",
        ("billing.checkout_downgrade_blocked",),
    ).fetchone()[0]
    assert n == 1
