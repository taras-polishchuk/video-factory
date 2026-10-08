"""Wave 6c \u2014 Stripe webhook handler + idempotency tests.

Targets \u00a73.3 of source-task.md. Coverage:

  * Signature verification: valid signature accepted, invalid
    signature rejected with 401, missing signature rejected.
  * Idempotency: replay of the same event id returns
    ``{duplicate: true}`` and does NOT double-apply the change.
  * Event dispatch:
      - ``checkout.session.completed``  -> plan=pro, customer_id, subscription_id
      - ``customer.subscription.updated`` -> plan + current_period_end
      - ``customer.subscription.deleted`` -> plan=free, clear subscription
      - ``invoice.payment_failed``        -> audit row, no plan change
      - unknown event type                 -> 200 {ignored: true}
  * Migrations: ``stripe_events`` table exists; 008 adds
    ``workspaces.current_period_end`` idempotently.

All tests build a TestClient against an in-tmppath repo + the
``STRIPE_WEBHOOK_SECRET`` env var set, then sign synthetic
events with ``stripe.Webhook.construct_event``'s own logic via
``stripe.Webhook.generate_test_header`` (the documented helper).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict

import pytest
import stripe
from fastapi.testclient import TestClient


WEBHOOK_PATH = "/api/v1/webhooks/stripe"
TEST_SECRET = "whsec_test_wave6c_local"
TEST_ENT_PRICE = "price_test_enterprise_123"

# Pricing for tests. By default the env var is empty so every
# subscription maps to ``pro``. Tests that exercise the
# enterprise path opt in via ``monkeypatch.setenv``.
ENTERPRISE_PRICE_ENV = "STRIPE_PRICE_ENTERPRISE"


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def stripe_env(tmp_path, monkeypatch):
    """Set up env + DB + TestClient for a Stripe webhook test."""
    db = tmp_path / "vf_6c.db"
    storage = tmp_path / "storage"
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(storage))
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", TEST_SECRET)
    # Avoid polluting test process with stale enterprise price.
    monkeypatch.delenv(ENTERPRISE_PRICE_ENV, raising=False)
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
    client = TestClient(app)
    return client, repo


@pytest.fixture
def workspace(stripe_env):
    """Create a workspace in the test DB and return its id."""
    _client, repo = stripe_env
    ws = repo.create_workspace(
        slug="wave6c-co", name="Wave 6C Co",
        owner_user_id=None, plan="free",
    )
    return ws["workspace_id"]


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

def _sign_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap ``payload`` in a Stripe-style header + return (body, header).

    The Stripe signature format is
    ``t=<unix_ts>,v1=<hmac_sha256(secret, "<ts>.<body>")>``.
    We sign manually because ``stripe.Webhook`` has no
    ``generate_test_header`` helper in 16.0.0 \u2014 the format
    itself is stable and tiny.
    """
    import hmac, hashlib
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = int(time.time())
    signed = f"{ts}.".encode() + body
    sig = hmac.new(TEST_SECRET.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    return {"body": body, "header": header}


def _checkout_completed(workspace_id: str, *,
                          customer_id: str = "cus_test_1",
                          subscription_id: str = "sub_test_1") -> Dict[str, Any]:
    return {
        "id": f"evt_co_{int(time.time() * 1000)}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "client_reference_id": workspace_id,
                "customer": customer_id,
                "subscription": subscription_id,
                "mode": "subscription",
                "payment_status": "paid",
            }
        },
    }


def _subscription_updated(*,
                            customer_id: str = "cus_test_1",
                            subscription_id: str = "sub_test_1",
                            period_end: int = 1735689600,
                            price_id: str = "price_test_pro_1") -> Dict[str, Any]:
    return {
        "id": f"evt_su_{int(time.time() * 1000)}",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": subscription_id,
                "customer": customer_id,
                "status": "active",
                "current_period_end": period_end,
                "items": {
                    "data": [
                        {"id": "si_test_1",
                         "price": {"id": price_id}}
                    ]
                },
            }
        },
    }


def _subscription_deleted(*,
                           customer_id: str = "cus_test_1",
                           subscription_id: str = "sub_test_1") -> Dict[str, Any]:
    return {
        "id": f"evt_sd_{int(time.time() * 1000)}",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": subscription_id,
                "customer": customer_id,
                "status": "canceled",
            }
        },
    }


def _invoice_payment_failed(*,
                              customer_id: str = "cus_test_1") -> Dict[str, Any]:
    return {
        "id": f"evt_pf_{int(time.time() * 1000)}",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "id": "in_test_1",
                "customer": customer_id,
                "amount_due": 1999,
                "currency": "usd",
            }
        },
    }


# ────────────────────────────────────────────────────────────────────
# 1. Signature verification
# ────────────────────────────────────────────────────────────────────

def test_stripe_webhook_signature_verification(stripe_env, workspace):
    """A correctly signed checkout.session.completed event
    is accepted, sets plan=pro, and writes a stripe_events row.
    """
    client, repo = stripe_env
    event = _checkout_completed(workspace)
    sig = _sign_payload(event)
    r = client.post(
        WEBHOOK_PATH,
        content=sig["body"],
        headers={"Stripe-Signature": sig["header"],
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["plan"] == "pro"
    assert body["workspace_id"] == workspace

    # Workspace was upgraded.
    ws = repo.get_workspace(workspace)
    assert ws["plan"] == "pro"
    assert ws["stripe_customer_id"] == "cus_test_1"
    assert ws["stripe_subscription_id"] == "sub_test_1"

    # Idempotency row was stamped processed.
    stored = repo.get_stripe_event(event["id"])
    assert stored is not None
    assert stored["type"] == "checkout.session.completed"
    assert stored["processed_at"] is not None


def test_stripe_webhook_invalid_signature_returns_401(stripe_env, workspace):
    """A request with a bogus signature is rejected with 401."""
    client, _ = stripe_env
    event = _checkout_completed(workspace)
    body = json.dumps(event).encode("utf-8")
    r = client.post(
        WEBHOOK_PATH,
        content=body,
        headers={"Stripe-Signature":
                    "t=1,v1=0000000000000000000000000000000000000000000000000000000000000000",
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 401, (r.status_code, r.text)
    # FastAPI's HTTPException(401, ...) renders as ``{"detail": ...}``
    # not as ``{"error": ...}``.  Accept either shape \u2014 the
    # contract is "401 with a human-readable explanation".
    payload = r.json()
    if "detail" in payload:
        msg = payload["detail"]
    elif "error" in payload:
        msg = payload["error"]
    else:
        msg = json.dumps(payload)
    assert "invalid" in msg.lower() or "signature" in msg.lower(), msg


def test_stripe_webhook_missing_signature_returns_401(stripe_env, workspace):
    """A request with no Stripe-Signature header is rejected."""
    client, _ = stripe_env
    event = _checkout_completed(workspace)
    body = json.dumps(event).encode("utf-8")
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 401


# ────────────────────────────────────────────────────────────────────
# 2. Event dispatch
# ────────────────────────────────────────────────────────────────────

def test_stripe_webhook_checkout_completed_updates_plan(stripe_env, workspace):
    """Checkout completion upgrades the workspace to pro and
    records customer + subscription ids.
    """
    client, repo = stripe_env
    event = _checkout_completed(
        workspace, customer_id="cus_aaa", subscription_id="sub_aaa",
    )
    sig = _sign_payload(event)
    r = client.post(
        WEBHOOK_PATH, content=sig["body"],
        headers={"Stripe-Signature": sig["header"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    ws = repo.get_workspace(workspace)
    assert ws["plan"] == "pro"
    assert ws["stripe_customer_id"] == "cus_aaa"
    assert ws["stripe_subscription_id"] == "sub_aaa"


def test_stripe_webhook_subscription_updated_updates_period_end(stripe_env, workspace):
    """customer.subscription.updated sets plan from price and
    stores the period end as an integer on the workspace.
    """
    client, repo = stripe_env
    # Pre-bind the workspace to a customer id so the event can
    # find it without going through checkout first.
    repo.update_workspace_billing(
        workspace,
        stripe_customer_id="cus_existing",
    )
    period_end = 1735689600
    event = _subscription_updated(
        customer_id="cus_existing",
        subscription_id="sub_existing",
        period_end=period_end,
    )
    sig = _sign_payload(event)
    r = client.post(
        WEBHOOK_PATH, content=sig["body"],
        headers={"Stripe-Signature": sig["header"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "pro"
    # Re-read via find_workspace_by_stripe_customer to confirm
    # current_period_end was stored.
    ws = repo.find_workspace_by_stripe_customer("cus_existing")
    assert ws is not None
    assert ws["current_period_end"] == period_end
    assert ws["plan"] == "pro"


def test_stripe_webhook_subscription_deleted_reverts_to_free(stripe_env, workspace):
    """customer.subscription.deleted reverts the plan to free
    and clears the subscription id.
    """
    client, repo = stripe_env
    # Start on pro so we can verify the downgrade.
    repo.update_workspace_billing(
        workspace, plan="pro",
        stripe_customer_id="cus_down",
        stripe_subscription_id="sub_down",
    )
    event = _subscription_deleted(
        customer_id="cus_down", subscription_id="sub_down",
    )
    sig = _sign_payload(event)
    r = client.post(
        WEBHOOK_PATH, content=sig["body"],
        headers={"Stripe-Signature": sig["header"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "free"
    ws = repo.get_workspace(workspace)
    assert ws["plan"] == "free"
    assert ws["stripe_subscription_id"] is None
    # customer id is preserved (still bound to a Stripe customer
    # record), only the subscription is cleared.
    assert ws["stripe_customer_id"] == "cus_down"


def test_stripe_webhook_payment_failed_writes_audit_row(stripe_env, workspace):
    """invoice.payment_failed writes a billing.payment_failed
    audit row but does NOT downgrade the plan (dunning V2).
    """
    client, repo = stripe_env
    repo.update_workspace_billing(
        workspace, plan="pro", stripe_customer_id="cus_pf",
    )
    event = _invoice_payment_failed(customer_id="cus_pf")
    sig = _sign_payload(event)
    r = client.post(
        WEBHOOK_PATH, content=sig["body"],
        headers={"Stripe-Signature": sig["header"]},
    )
    assert r.status_code == 200, r.text
    # Plan is preserved.
    ws = repo.get_workspace(workspace)
    assert ws["plan"] == "pro"
    # Audit row was written.
    rows = repo._conn().execute(
        "SELECT event, context_json, workspace_id FROM audit_log "
        "WHERE event='billing.invoice.payment_failed' "
        "ORDER BY created_at DESC LIMIT 1",
    ).fetchone()
    assert rows is not None, "expected billing.invoice.payment_failed audit row"
    ctx = json.loads(rows[1])
    assert ctx["workspace_id"] == workspace
    assert ctx["stripe_customer_id"] == "cus_pf"
    assert rows[2] == workspace


# ────────────────────────────────────────────────────────────────────
# 3. Idempotency
# ────────────────────────────────────────────────────────────────────

def test_stripe_webhook_idempotency_replay_safe(stripe_env, workspace):
    """Re-sending the same event id returns {duplicate: true}
    and does NOT re-apply the change.
    """
    client, repo = stripe_env
    # Bind customer to workspace.
    repo.update_workspace_billing(
        workspace, stripe_customer_id="cus_replay",
    )
    event = _subscription_updated(
        customer_id="cus_replay", subscription_id="sub_replay",
        period_end=1700000000, price_id="price_pro",
    )
    sig = _sign_payload(event)
    r1 = client.post(
        WEBHOOK_PATH, content=sig["body"],
        headers={"Stripe-Signature": sig["header"]},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json().get("duplicate") is not True

    # Capture state after first delivery.
    ws_after_first = repo.find_workspace_by_stripe_customer("cus_replay")
    first_cpe = ws_after_first["current_period_end"]

    # Replay the same event.
    r2 = client.post(
        WEBHOOK_PATH, content=sig["body"],
        headers={"Stripe-Signature": sig["header"]},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2.get("duplicate") is True
    assert body2.get("event_id") == event["id"]

    # Workspace state did not change.
    ws_after_replay = repo.find_workspace_by_stripe_customer("cus_replay")
    assert ws_after_replay["current_period_end"] == first_cpe
    # stripe_events table still has exactly one row.
    cnt = repo._conn().execute(
        "SELECT COUNT(*) FROM stripe_events WHERE id=?",
        (event["id"],),
    ).fetchone()[0]
    assert cnt == 1


# ────────────────────────────────────────────────────────────────────
# 4. Unknown event types
# ────────────────────────────────────────────────────────────────────

def test_stripe_webhook_unknown_event_type_acknowledged(stripe_env):
    """An event type we don't handle is acked with 200 +
    {ignored: true} so Stripe stops retrying.
    """
    client, repo = stripe_env
    event = {
        "id": f"evt_unknown_{int(time.time() * 1000)}",
        "type": "customer.tax.updated",  # not in HANDLED_EVENT_TYPES
        "data": {"object": {"id": "tx_test"}},
    }
    sig = _sign_payload(event)
    r = client.post(
        WEBHOOK_PATH, content=sig["body"],
        headers={"Stripe-Signature": sig["header"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ignored") is True
    assert body.get("type") == "customer.tax.updated"
    # The event is still recorded (so a future delivered event
    # of the same id would be deduped) and stamped processed.
    stored = repo.get_stripe_event(event["id"])
    assert stored is not None
    assert stored["processed_at"] is not None


# ────────────────────────────────────────────────────────────────────
# 5. Migrations (defense in depth)
# ────────────────────────────────────────────────────────────────────

def test_stripe_events_table_exists(stripe_env):
    """Migration 009 created the stripe_events table with the
    documented schema.
    """
    _client, repo = stripe_env
    cols = repo._table_columns("stripe_events")
    assert {"id", "type", "received_at", "raw_json",
            "processed_at"}.issubset(cols)


def test_workspaces_current_period_end_column_exists(stripe_env):
    """Migration 008 + _extend_workspaces_columns added the
    current_period_end column on workspaces.
    """
    _client, repo = stripe_env
    cols = repo._table_columns("workspaces")
    assert "current_period_end" in cols


# ────────────────────────────────────────────────────────────────────
# 6. Enterprise price mapping (one extra, cheap)
# ────────────────────────────────────────────────────────────────────

def test_stripe_webhook_enterprise_price_maps_to_enterprise_plan(
    stripe_env, workspace, monkeypatch,
):
    """When STRIPE_PRICE_ENTERPRISE matches the subscription's
    price id, customer.subscription.updated upgrades the
    workspace to the enterprise plan instead of pro.
    """
    client, repo = stripe_env
    monkeypatch.setenv(ENTERPRISE_PRICE_ENV, TEST_ENT_PRICE)
    repo.update_workspace_billing(
        workspace, stripe_customer_id="cus_ent",
    )
    event = _subscription_updated(
        customer_id="cus_ent", subscription_id="sub_ent",
        price_id=TEST_ENT_PRICE, period_end=1800000000,
    )
    sig = _sign_payload(event)
    r = client.post(
        WEBHOOK_PATH, content=sig["body"],
        headers={"Stripe-Signature": sig["header"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "enterprise"
    ws = repo.find_workspace_by_stripe_customer("cus_ent")
    assert ws["plan"] == "enterprise"


# ────────────────────────────────────────────────────────────────────
# 7. Boot-time config fail-loud
# ────────────────────────────────────────────────────────────────────

def test_app_fails_loud_without_stripe_webhook_secret(tmp_path, monkeypatch):
    """When STRIPE_WEBHOOK_SECRET is unset, create_app() still
    succeeds (the app can serve other routes) BUT the
    ``/_stripe_init_error`` debug endpoint surfaces the missing
    config and the ``/api/v1/webhooks/stripe`` route is NOT
    registered (Stripe's traffic gets a 404 from us rather than
    a misleading 503).
    """
    db = tmp_path / "vf_no_secret.db"
    storage = tmp_path / "storage"
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(storage))
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton
    repo = Repo(str(db))
    runner = JobRunner(
        repo=repo, storage_root=str(storage), schema={},
        budget_per_video_usd=0.0, budget_per_batch_usd=0.0,
    )
    Singleton.set(runner)
    app = create_app(
        repo=repo, runner=runner, storage_root=str(storage), schema={},
    )
    client = TestClient(app)
    # Debug endpoint exposes the missing config.
    r = client.get("/_stripe_init_error")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "STRIPE_WEBHOOK_SECRET" in r.json()["error"]
    # Webhook route is NOT registered.
    r2 = client.post("/api/v1/webhooks/stripe", content=b"{}")
    assert r2.status_code == 404
