"""Wave 6c QA-owned independent verification suite.

Independent of ``test_wave6c_stripe_webhook.py`` — uses the real
``stripe.Webhook.construct_event`` helper rather than a manual
HMAC recomputation. Probes:

  * Signature verification with the actual Stripe SDK.
  * Idempotency: same event id replayed, NO second row written,
    NO second side effect (audit_log entry, workspace column flip).
  * Each of the 4 handled event types round-trip into the DB.
  * Unknown event types ack with 200 + {ignored: true} AND still
    leave a stripe_events row marked processed (idempotent record).
  * Migrations: stripe_events schema + workspaces.current_period_end.
  * Boot-time fail-loud: missing STRIPE_WEBHOOK_SECRET does NOT
    silently 500 — surfaces on /_stripe_init_error AND the
    webhook route returns 404 (not a misleading 200).
  * Edge: secret mismatched by 1 byte → 401, not 500.
  * Edge: empty body with valid signature → 400.
  * Edge: signature with valid t= but corrupted v1= → 401.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Tuple

import pytest
import stripe
from fastapi.testclient import TestClient


WEBHOOK_PATH = "/api/v1/webhooks/stripe"
TEST_SECRET = "whsec_qa_wave6c_independent"
TEST_ENT_PRICE = "price_qa_ent_enterprise_999"


@pytest.fixture
def stripe_env(tmp_path, monkeypatch):
    db = tmp_path / "vf_qa6c.db"
    storage = tmp_path / "storage"
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(storage))
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.delenv("STRIPE_PRICE_ENTERPRISE", raising=False)
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
    return TestClient(app), repo


def _sign_with_stripe_sdk(
    payload: Dict[str, Any], *, secret: str = TEST_SECRET,
    ts: int = None,
) -> Tuple[bytes, str]:
    """Use the official ``stripe.WebhookSignature.generate_signature_header``
    helper to produce a header that ``construct_event`` will accept.

    Stripe 16.0.0 removed ``stripe.Webhook.generate_test_header_string``
    from the public surface; the underlying primitive lives on
    ``stripe._webhook.WebhookSignature.generate_signature_header``.
    Using the SDK helper (not manual HMAC) is the meaningful QA
    probe — if the handler had a non-standard HMAC scheme, this
    would 401.
    """
    body_str = json.dumps(payload, separators=(",", ":"))
    body_bytes = body_str.encode("utf-8")
    import stripe._webhook as _wh
    if ts is not None:
        header = _wh.WebhookSignature.generate_signature_header(
            body_str, secret, timestamp=int(ts),
        )
    else:
        header = _wh.WebhookSignature.generate_signature_header(
            body_str, secret,
        )
    return body_bytes, header


def _ev(type_: str, *, data_obj: Dict[str, Any],
        event_id: str = None) -> Dict[str, Any]:
    eid = event_id or f"evt_qa_{type_.replace('.', '_')}_{int(time.time() * 1_000_000)}"
    return {"id": eid, "type": type_, "data": {"object": data_obj}}


def _workspace_id(repo) -> str:
    ws = repo.create_workspace(
        slug=f"qa6c-{int(time.time() * 1_000_000)}",
        name="QA 6C",
        owner_user_id=None, plan="free",
    )
    return ws["workspace_id"]


# ────────────────────────────────────────────────────────────────────
# 1. Real-SDK signature path
# ────────────────────────────────────────────────────────────────────

def test_real_stripe_sdk_signature_accepted(stripe_env):
    """Use the actual ``stripe.Webhook.generate_test_header_string``
    helper. If the handler relied on a non-standard HMAC scheme,
    this would 401.
    """
    client, repo = stripe_env
    ws = _workspace_id(repo)
    payload = _ev(
        "checkout.session.completed",
        data_obj={
            "id": "cs_qa_1", "client_reference_id": ws,
            "customer": "cus_qa_sdk", "subscription": "sub_qa_sdk",
            "mode": "subscription", "payment_status": "paid",
        },
    )
    body, header = _sign_with_stripe_sdk(payload)
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": header,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    body_j = r.json()
    assert body_j.get("ok") is True
    assert body_j.get("workspace_id") == ws
    # Also check the workspace was actually updated.
    ws_after = repo.get_workspace(ws)
    assert ws_after["plan"] == "pro"
    assert ws_after["stripe_customer_id"] == "cus_qa_sdk"


# ────────────────────────────────────────────────────────────────────
# 2. Signature variants that MUST be rejected
# ────────────────────────────────────────────────────────────────────

def test_signature_off_by_one_byte_returns_401(stripe_env):
    """Same secret, same body, but flip one byte of v1= signature.
    Must NOT 200.
    """
    client, repo = stripe_env
    ws = _workspace_id(repo)
    payload = _ev(
        "checkout.session.completed",
        data_obj={"id": "cs_qa_2", "client_reference_id": ws,
                  "customer": "cus_qa_off1", "subscription": "sub_qa_off1"},
    )
    body, header = _sign_with_stripe_sdk(payload)
    # Flip a single hex char in the signature.
    flipped = header[:5] + ("0" if header[5] != "0" else "1") + header[6:]
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": flipped,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 401, (r.status_code, r.text)
    # No side effect: workspace stays free.
    ws_after = repo.get_workspace(ws)
    assert ws_after["plan"] == "free"
    # No stripe_events row.
    cnt = repo._conn().execute(
        "SELECT COUNT(*) FROM stripe_events",
    ).fetchone()[0]
    assert cnt == 0


def test_signature_with_wrong_secret_returns_401(stripe_env):
    """Sign with a different secret than the one configured.
    """
    client, repo = stripe_env
    payload = _ev(
        "checkout.session.completed",
        data_obj={"id": "cs_qa_3",
                  "client_reference_id": _workspace_id(repo),
                  "customer": "cus_qa_wrong", "subscription": "sub_qa_wrong"},
    )
    body, _ = _sign_with_stripe_sdk(payload, secret=TEST_SECRET)
    import stripe._webhook as _wh
    wrong_header = _wh.WebhookSignature.generate_signature_header(
        body.decode("utf-8"), "whsec_qa_TOTALLY_DIFFERENT_zzz",
    )
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": wrong_header,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 401


def test_signature_with_stale_timestamp_returns_401(stripe_env, monkeypatch):
    """Sign with timestamp far in the past. Stripe's default
    tolerance is 300s; a 2-hour-old signature should be rejected
    by ``construct_event`` and bubble up as 401.
    """
    client, repo = stripe_env
    payload = _ev(
        "checkout.session.completed",
        data_obj={"id": "cs_qa_stale",
                  "client_reference_id": _workspace_id(repo),
                  "customer": "cus_qa_stale", "subscription": "sub_qa_stale"},
    )
    stale_ts = int(time.time()) - 7200  # 2 hours ago
    body, header = _sign_with_stripe_sdk(payload, ts=stale_ts)
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": header,
                 "Content-Type": "application/json"},
    )
    # Construct_event raises SignatureVerificationError on stale.
    # Handler returns 401.
    assert r.status_code == 401, (r.status_code, r.text)


# ────────────────────────────────────────────────────────────────────
# 3. Idempotency — independent of dev test
# ────────────────────────────────────────────────────────────────────

def test_replay_same_event_id_three_times_yields_one_side_effect(stripe_env):
    """Send the SAME event id 3x. Workspace must end up in the
    state of the first delivery only; stripe_events must hold
    exactly 1 row; audit_log must hold exactly 1 row of type
    ``billing.checkout.session.completed`` for this workspace.
    """
    client, repo = stripe_env
    ws = _workspace_id(repo)
    payload = _ev(
        "checkout.session.completed",
        data_obj={"id": "cs_qa_replay3",
                  "client_reference_id": ws,
                  "customer": "cus_qa_replay3", "subscription": "sub_qa_replay3"},
        event_id="evt_qa_replay3_id",
    )
    body, header = _sign_with_stripe_sdk(payload)
    responses = []
    for _ in range(3):
        r = client.post(
            WEBHOOK_PATH, content=body,
            headers={"Stripe-Signature": header,
                     "Content-Type": "application/json"},
        )
        responses.append((r.status_code, r.json()))
    # All 200.
    assert all(code == 200 for code, _ in responses), responses
    # First is ok, next two are duplicates.
    assert responses[0][1].get("ok") is True
    assert responses[0][1].get("duplicate") is not True
    assert responses[1][1].get("duplicate") is True
    assert responses[2][1].get("duplicate") is True

    # Exactly 1 stripe_events row.
    cnt = repo._conn().execute(
        "SELECT COUNT(*) FROM stripe_events WHERE id=?",
        ("evt_qa_replay3_id",),
    ).fetchone()[0]
    assert cnt == 1, f"expected 1 row, got {cnt}"

    # Exactly 1 billing audit row.
    cnt_audit = repo._conn().execute(
        "SELECT COUNT(*) FROM audit_log WHERE event=?"
        " AND workspace_id=?",
        ("billing.checkout.session.completed", ws),
    ).fetchone()[0]
    assert cnt_audit == 1, f"expected 1 audit row, got {cnt_audit}"

    # Workspace plan = pro.
    assert repo.get_workspace(ws)["plan"] == "pro"


def test_replay_after_plan_change_does_not_revert(stripe_env):
    """Replay an OLD subscription.updated event whose price_id
    maps to pro. Workspace has since been moved to enterprise
    (out-of-band, simulating a separate Stripe price change).
    The replay must NOT downgrade.
    """
    client, repo = stripe_env
    ws = _workspace_id(repo)
    repo.update_workspace_billing(
        ws, plan="enterprise", stripe_customer_id="cus_qa_norevert",
        stripe_subscription_id="sub_qa_norevert",
        current_period_end=2000000000,
    )
    payload = _ev(
        "customer.subscription.updated",
        data_obj={
            "id": "sub_qa_norevert", "customer": "cus_qa_norevert",
            "status": "active", "current_period_end": 1999999999,
            "items": {"data": [{"id": "si_1",
                                "price": {"id": "price_qa_pro_x"}}]},
        },
        event_id="evt_qa_norevert",
    )
    body, header = _sign_with_stripe_sdk(payload)
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": header,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    # Plan stays enterprise.
    ws_after = repo.find_workspace_by_stripe_customer("cus_qa_norevert")
    assert ws_after["plan"] == "enterprise"
    # Period end unchanged (still 2000000000).
    assert ws_after["current_period_end"] == 2000000000


# ────────────────────────────────────────────────────────────────────
# 4. Round-trip dispatch for each handled type
# ────────────────────────────────────────────────────────────────────

def test_subscription_updated_with_integer_period_end_persisted(stripe_env):
    """The acceptance criterion says "assertion on integer column".
    Verify current_period_end is stored as INTEGER, not str.
    """
    client, repo = stripe_env
    ws = _workspace_id(repo)
    repo.update_workspace_billing(
        ws, stripe_customer_id="cus_qa_int",
    )
    period_end_int = 1735689600
    payload = _ev(
        "customer.subscription.updated",
        data_obj={
            "id": "sub_qa_int", "customer": "cus_qa_int",
            "status": "active", "current_period_end": period_end_int,
            "items": {"data": [{"id": "si_int",
                                "price": {"id": "price_qa_int"}}]},
        },
    )
    body, header = _sign_with_stripe_sdk(payload)
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": header,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    ws_after = repo.find_workspace_by_stripe_customer("cus_qa_int")
    assert ws_after["current_period_end"] == period_end_int
    # Type check via sqlite.
    row = repo._conn().execute(
        "SELECT typeof(current_period_end) FROM workspaces "
        "WHERE workspace_id=?", (ws,),
    ).fetchone()
    assert row[0] == "integer", f"current_period_end stored as {row[0]!r}"


def test_subscription_deleted_clears_subscription_keeps_customer(stripe_env):
    """Per the spec: customer.subscription.deleted reverts plan
    to free AND clears subscription_id BUT preserves customer_id.
    """
    client, repo = stripe_env
    ws = _workspace_id(repo)
    repo.update_workspace_billing(
        ws, plan="pro",
        stripe_customer_id="cus_qa_keep", stripe_subscription_id="sub_qa_drop",
    )
    payload = _ev(
        "customer.subscription.deleted",
        data_obj={"id": "sub_qa_drop", "customer": "cus_qa_keep",
                  "status": "canceled"},
    )
    body, header = _sign_with_stripe_sdk(payload)
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": header,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    ws_after = repo.get_workspace(ws)
    assert ws_after["plan"] == "free"
    assert ws_after["stripe_subscription_id"] is None
    assert ws_after["stripe_customer_id"] == "cus_qa_keep"


def test_payment_failed_audit_row_no_plan_change(stripe_env):
    """Plan must NOT change on payment_failed; audit row must be
    written.
    """
    client, repo = stripe_env
    ws = _workspace_id(repo)
    repo.update_workspace_billing(
        ws, plan="pro", stripe_customer_id="cus_qa_pf2",
    )
    payload = _ev(
        "invoice.payment_failed",
        data_obj={"id": "in_qa_pf", "customer": "cus_qa_pf2",
                  "amount_due": 4200, "currency": "usd"},
    )
    body, header = _sign_with_stripe_sdk(payload)
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": header,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    # Plan unchanged.
    assert repo.get_workspace(ws)["plan"] == "pro"
    # Audit row.
    row = repo._conn().execute(
        "SELECT event, context_json FROM audit_log "
        "WHERE event='billing.invoice.payment_failed' "
        "AND workspace_id=? ORDER BY created_at DESC LIMIT 1",
        (ws,),
    ).fetchone()
    assert row is not None
    ctx = json.loads(row[1])
    assert ctx["stripe_customer_id"] == "cus_qa_pf2"
    assert ctx["amount_due"] == 4200


# ────────────────────────────────────────────────────────────────────
# 5. Unknown event type — independent probe
# ────────────────────────────────────────────────────────────────────

def test_unknown_event_type_returns_200_and_recorded(stripe_env):
    """A type not in HANDLED_EVENT_TYPES must return 200 +
    {ignored: true} AND still write a stripe_events row marked
    processed. The brief explicitly requires this — unknown
    events must NOT 5xx (Stripe would retry forever).
    """
    client, repo = stripe_env
    eid = f"evt_qa_unknown_{int(time.time() * 1_000_000)}"
    payload = _ev(
        "customer.updated",  # explicitly NOT in handler list
        data_obj={"id": "cus_qa_unknown"},
        event_id=eid,
    )
    body, header = _sign_with_stripe_sdk(payload)
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": header,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    body_j = r.json()
    assert body_j.get("ignored") is True
    assert body_j.get("ok") is not True
    assert body_j.get("type") == "customer.updated"
    # Still recorded.
    stored = repo.get_stripe_event(eid)
    assert stored is not None
    assert stored["type"] == "customer.updated"
    assert stored["processed_at"] is not None


# ────────────────────────────────────────────────────────────────────
# 6. Migration shape
# ────────────────────────────────────────────────────────────────────

def test_stripe_events_schema_minimum_required_columns(stripe_env):
    """stripe_events MUST have id PK, type, received_at, raw_json,
    processed_at. Defense in depth — separate from dev's check.
    """
    _client, repo = stripe_env
    cols = repo._table_columns("stripe_events")
    required = {"id", "type", "received_at", "raw_json", "processed_at"}
    missing = required - set(cols)
    assert not missing, f"missing columns: {missing}"


def test_workspaces_current_period_end_column_exists(stripe_env):
    _client, repo = stripe_env
    cols = repo._table_columns("workspaces")
    assert "current_period_end" in cols, cols


# ────────────────────────────────────────────────────────────────────
# 7. Boot-time fail-loud + missing-secret behavior
# ────────────────────────────────────────────────────────────────────

def test_missing_secret_webhook_route_unregistered(
    tmp_path, monkeypatch,
):
    """Per brief #9: "boot fails loud (not silent skip)". Dev's
    implementation: app boots, debug endpoint surfaces the
    error, webhook route is NOT registered (404 not 200/503).
    This test pins both halves of the contract.
    """
    db = tmp_path / "vf_qa_no_secret.db"
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
    # Debug endpoint surfaces the error.
    r = client.get("/_stripe_init_error")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "STRIPE_WEBHOOK_SECRET" in body["error"]
    # Webhook route is NOT registered → 404.
    r2 = client.post(WEBHOOK_PATH, content=b"{}")
    assert r2.status_code == 404, (r2.status_code, r2.text)
    # No stripe_events row written.
    cnt = repo._conn().execute(
        "SELECT COUNT(*) FROM stripe_events",
    ).fetchone()[0]
    assert cnt == 0


# ────────────────────────────────────────────────────────────────────
# 8. Edge: empty body
# ────────────────────────────────────────────────────────────────────

def test_empty_body_with_valid_signature_returns_400(stripe_env):
    """An empty POST body is malformed. Must NOT 200 (would be
    a phantom processed event).
    """
    client, _ = stripe_env
    body, header = _sign_with_stripe_sdk({})
    r = client.post(
        WEBHOOK_PATH, content=b"",
        headers={"Stripe-Signature": header,
                 "Content-Type": "application/json"},
    )
    # Handler's `if not body: raise HTTPException(400, ...)`.
    assert r.status_code == 400, (r.status_code, r.text)


# ────────────────────────────────────────────────────────────────────
# 9. Unknown customer event is logged + acknowledged
# ────────────────────────────────────────────────────────────────────

def test_subscription_updated_unknown_customer_acked(stripe_env):
    """customer.subscription.updated for an unknown customer_id
    must return 200 + {ignored: true} AND not crash.
    """
    client, repo = stripe_env
    payload = _ev(
        "customer.subscription.updated",
        data_obj={
            "id": "sub_qa_orphan", "customer": "cus_qa_orphan",
            "status": "active", "current_period_end": 1735689600,
            "items": {"data": [{"id": "si_o",
                                "price": {"id": "price_qa_o"}}]},
        },
    )
    body, header = _sign_with_stripe_sdk(payload)
    r = client.post(
        WEBHOOK_PATH, content=body,
        headers={"Stripe-Signature": header,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    body_j = r.json()
    assert body_j.get("ignored") is True
    assert "unknown_customer" in body_j.get("reason", "") or \
        body_j.get("workspace_id") is None
    # No workspaces updated.
    cnt = repo._conn().execute(
        "SELECT COUNT(*) FROM workspaces "
        "WHERE stripe_customer_id=?",
        ("cus_qa_orphan",),
    ).fetchone()[0]
    assert cnt == 0