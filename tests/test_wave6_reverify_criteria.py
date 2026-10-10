"""Wave 6 / 6a / 6b / 6c re-verify (t_f22629ed): acceptance-criteria walk on main.

One real FastAPI app (TestClient), one real signup/login/CSRF session, and a
Stripe test-mode-shaped fake: SDK calls (`stripe.checkout.Session.create`,
`stripe.billing_portal.Session.create`) are patched (no network); webhook
payloads are signed with the real Stripe SDK signature helper against a test
secret, so `stripe.Webhook.construct_event` runs for real.

Each test name starts with the criterion id used in the re-verify report.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Dict, Tuple
from unittest.mock import MagicMock, patch

_DEFAULT_CSRF = None

import pytest
from fastapi.testclient import TestClient

WEBHOOK_PATH = "/api/v1/webhooks/stripe"
WHSEC = "whsec_reverify_criteria"
ENT_PRICE = "price_rv_enterprise"
PRO_PRICE = "price_rv_pro"


@pytest.fixture()
def e2e(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(tmp_path / "storage"))
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_reverify")
    monkeypatch.setenv("STRIPE_PRO_PRICE_ID", PRO_PRICE)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WHSEC)
    monkeypatch.setenv("STRIPE_PRICE_ENTERPRISE", ENT_PRICE)
    for k in ("VF_HTTPS", "VIDEO_FACTORY_ENV", "LIVE_PROVIDER_TESTS",
              "SMOKE_TEST_PASSED"):
        monkeypatch.delenv(k, raising=False)

    import importlib
    importlib.reload(importlib.import_module("auth.ratelimit"))
    from api import billing as _billing
    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton

    repo = Repo(str(tmp_path / "vf.db"))
    runner = JobRunner(
        repo=repo, storage_root=str(tmp_path / "storage"), schema={},
        budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
    )
    Singleton.set(runner)
    app = create_app(repo=repo, runner=runner,
                     storage_root=str(tmp_path / "storage"), schema={})
    client = TestClient(app)
    _billing.reset_stripe_client()

    r = client.post("/auth/signup", json={
        "email": "rv-owner@example.com", "password": "ReverifyPass-1234!",
        "workspace_name": "RV Co",
    })
    assert r.status_code == 200, r.text
    wid = r.json()["workspace_id"]
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    import sys as _sys; _sys.modules[__name__]._DEFAULT_CSRF = csrf
    yield client, repo, wid, csrf
    _billing.reset_stripe_client()
    # expose for the helpers via module global (set on fixture setup)


# --- helpers -----------------------------------------------------------------

def _sig(payload: Dict[str, Any], secret: str = WHSEC,
         ts: int | None = None) -> Tuple[bytes, str]:
    import stripe._webhook as _wh

    body = json.dumps(payload, separators=(",", ":"))
    kwargs = {"timestamp": ts} if ts is not None else {}
    header = _wh.WebhookSignature.generate_signature_header(body, secret, **kwargs)
    return body.encode(), header


def _hook(client, payload, **kw):
    body, header = _sig(payload, **kw)
    return client.post(WEBHOOK_PATH, content=body, headers={
        "Stripe-Signature": header, "Content-Type": "application/json"})


def _checkout_fake(customer="cus_rv_1", session="cs_test_rv_1"):
    s = MagicMock()
    s.url = f"https://checkout.stripe.com/c/{session}"
    s.id = session
    s.customer = customer
    return s


def _portal_fake():
    s = MagicMock()
    s.url = "https://billing.stripe.com/p/rv"
    s.id = "bps_test_rv"
    return s


def _checkout_evt(eid, wid, cus, sub):
    return {"id": eid, "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_test_rv", "client_reference_id": wid, "customer": cus,
        "subscription": sub}}}


def _sub_evt(eid, cus, sub, price):
    return {"id": eid, "type": "customer.subscription.updated", "data": {"object": {
        "id": sub, "customer": cus, "status": "active",
        "current_period_end": 2100000000,
        "items": {"data": [{"id": "si_1", "price": {"id": price}}]}}}}


def _audit(repo, wid, event):
    return [r for r in repo.list_audit(workspace_id=wid, limit=200)
            if r["event"] == event]


def _audit_any(repo, event):
    return repo._conn().execute(
        "SELECT COUNT(*) FROM audit_log WHERE event=?", (event,)).fetchone()[0]


def _fill_jobs(repo, wid, n):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with repo.tx() as c:
        for i in range(n):
            c.execute(
                "INSERT INTO jobs(job_id, workspace_id, bible_id, topic, language, "
                "duration_seconds, platforms_json, aspect_ratio, count, render_profile, "
                "approval_required, created_at, updated_at, state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"job_{secrets.token_hex(6)}", wid, None, f"rv-{i}", "en", 5,
                 '["tiktok"]', "9:16", 1, "mock", 0, now, now, "completed"))


def _submit(client, wid, profile="mock", csrf=None):
    token = csrf if csrf is not None else _DEFAULT_CSRF
    headers = {"X-CSRF-Token": token} if token else {}
    return client.post(f"/api/v1/workspaces/{wid}/jobs", json={
        "topic": "t", "language": "en", "duration_seconds": 5,
        "platforms": ["tiktok"], "render_profile": profile, "count": 1},
        headers=headers)


# --- C1 / C2: checkout + portal (Wave 6a) ------------------------------------

def test_C1_checkout_stripe_error_is_502_with_failed_audit(e2e):
    from stripe._error import StripeError
    client, repo, wid, csrf = e2e
    with patch("stripe.checkout.Session.create", side_effect=StripeError("boom")):
        r = client.post("/api/v1/billing/checkout",
                        json={"workspace_id": wid, "plan": "pro"},
                        headers={"X-CSRF-Token": csrf})
    assert r.status_code == 502, r.text
    rows = _audit(repo, wid, "billing.checkout_failed")
    assert len(rows) == 1
    assert rows[0]["context"]["workspace_id"] == wid


def test_C2_checkout_ok_then_portal_ok_and_portal_error_is_502(e2e):
    from stripe._error import StripeError
    client, repo, wid, csrf = e2e
    h = {"X-CSRF-Token": csrf}
    # portal before any customer -> 404
    assert client.post("/api/v1/billing/portal", json={"workspace_id": wid},
                       headers=h).status_code == 404
    with patch("stripe.checkout.Session.create", return_value=_checkout_fake()):
        r = client.post("/api/v1/billing/checkout",
                        json={"workspace_id": wid, "plan": "pro"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.com/")
    assert repo.get_workspace_billing(wid)["stripe_customer_id"] == "cus_rv_1"
    with patch("stripe.billing_portal.Session.create",
               return_value=_portal_fake()) as m:
        r = client.post("/api/v1/billing/portal", json={"workspace_id": wid},
                        headers=h)
    assert r.status_code == 200, r.text
    assert m.call_args.kwargs["customer"] == "cus_rv_1"
    with patch("stripe.billing_portal.Session.create",
               side_effect=StripeError("portal boom")):
        r = client.post("/api/v1/billing/portal", json={"workspace_id": wid},
                        headers=h)
    assert r.status_code == 502, r.text
    assert len(_audit(repo, wid, "billing.portal_failed")) == 1


# --- C3 / C4: quotas + live gate (Wave 6b, F-W6QA-001) -----------------------

def test_C3_free_quota_five_jobs_then_429_on_sixth(e2e):
    client, repo, wid, _ = e2e
    _fill_jobs(repo, wid, 5)
    r = _submit(client, wid)
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["jobs_used"] == 5 and body["jobs_remaining"] == 0
    assert body["quota_reset_at"].endswith("T00:00:00Z")
    assert len(_audit(repo, wid, "quota.exceeded")) == 1


def test_C3b_free_quota_fifth_job_still_allowed(e2e):
    client, repo, wid, _ = e2e
    _fill_jobs(repo, wid, 4)
    r = _submit(client, wid)
    assert r.status_code == 200, r.text


def test_C4_free_live_submit_is_blocked_403_and_audited(e2e):
    client, repo, wid, _ = e2e
    repo.update_workspace(wid, live_enabled=True)
    r = _submit(client, wid, profile="heygen_direct")
    assert r.status_code in (402, 403), r.text
    assert len(_audit(repo, wid, "live_provider.blocked")) == 1


# --- C5: webhook signature (Wave 6c) -----------------------------------------

def test_C5_webhook_rejects_missing_wrong_secret_tampered_and_stale(e2e):
    client, repo, wid, _ = e2e
    evt = _checkout_evt("evt_rv_sig", wid, "cus_x", "sub_x")
    body, good = _sig(evt)
    # missing header
    assert client.post(WEBHOOK_PATH, content=body).status_code == 401
    # wrong secret
    b2, bad = _sig(evt, secret="whsec_attacker")
    assert client.post(WEBHOOK_PATH, content=b2,
                       headers={"Stripe-Signature": bad}).status_code == 401
    # tampered body
    assert client.post(WEBHOOK_PATH, content=body.replace(b"cus_x", b"cus_y"),
                       headers={"Stripe-Signature": good}).status_code == 401
    # stale timestamp (replay window)
    b3, old = _sig(evt, ts=int(time.time()) - 3600)
    assert client.post(WEBHOOK_PATH, content=b3,
                       headers={"Stripe-Signature": old}).status_code == 401
    assert repo.get_workspace(wid)["plan"] == "free"
    n = repo._conn().execute("SELECT COUNT(*) FROM stripe_events").fetchone()[0]
    assert n == 0


# --- C6: replay idempotency --------------------------------------------------

def test_C6_webhook_replay_is_idempotent(e2e):
    client, repo, wid, _ = e2e
    evt = _checkout_evt("evt_rv_replay", wid, "cus_rp", "sub_rp")
    r1 = _hook(client, evt)
    r2 = _hook(client, evt)
    r3 = _hook(client, evt)
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert r1.json().get("ok") is True
    assert r2.json().get("duplicate") is True and r3.json().get("duplicate") is True
    assert repo._conn().execute(
        "SELECT COUNT(*) FROM stripe_events WHERE id=?", ("evt_rv_replay",)
    ).fetchone()[0] == 1
    assert len(_audit(repo, wid, "billing.checkout.session.completed")) == 1


# --- C7: downgrade guard (F-W6C-001 + F-RV-001) -------------------------------

def test_C7_downgrade_guard_covers_subscription_updated_and_checkout(e2e):
    client, repo, wid, _ = e2e
    repo.update_workspace_billing(wid, plan="enterprise", stripe_customer_id="cus_dg",
                                  stripe_subscription_id="sub_dg",
                                  current_period_end=2000000000)
    r = _hook(client, _sub_evt("evt_rv_dg1", "cus_dg", "sub_dg", PRO_PRICE))
    assert r.status_code == 200 and r.json().get("downgrade_refused") is True
    assert repo.get_workspace(wid)["plan"] == "enterprise"
    # replay of the same refused event stays a no-op
    assert _hook(client, _sub_evt("evt_rv_dg1", "cus_dg", "sub_dg",
                                  PRO_PRICE)).json().get("duplicate") is True
    # late checkout.session.completed with a FRESH id must not demote either
    r = _hook(client, _checkout_evt("evt_rv_dg2", wid, "cus_dg", "sub_dg"))
    assert r.status_code == 200 and r.json().get("downgrade_refused") is True
    assert repo.get_workspace(wid)["plan"] == "enterprise"
    assert len(_audit(repo, wid, "billing.subscription_downgrade_blocked")) == 1
    assert len(_audit(repo, wid, "billing.checkout_downgrade_blocked")) == 1


# --- C8: full upgrade path ---------------------------------------------------

def test_C8_upgrade_path_free_checkout_pro_quota_lifted_enterprise_then_cancel(e2e):
    client, repo, wid, csrf = e2e
    h = {"X-CSRF-Token": csrf}
    assert repo.get_workspace(wid)["plan"] == "free"
    _fill_jobs(repo, wid, 5)
    assert _submit(client, wid).status_code == 429          # free is capped
    with patch("stripe.checkout.Session.create",
               return_value=_checkout_fake("cus_up", "cs_test_up")):
        assert client.post("/api/v1/billing/checkout",
                           json={"workspace_id": wid, "plan": "pro"},
                           headers=h).status_code == 200
    assert repo.get_workspace(wid)["plan"] == "free"        # not pro until webhook
    r = _hook(client, _checkout_evt("evt_rv_up1", wid, "cus_up", "sub_up"))
    assert r.status_code == 200 and r.json()["plan"] == "pro"
    ws = repo.get_workspace(wid)
    assert ws["plan"] == "pro" and ws["stripe_subscription_id"] == "sub_up"
    assert _submit(client, wid).status_code == 200          # quota lifted
    # already-paid workspace is sent to the portal, not back into checkout
    with patch("stripe.checkout.Session.create", return_value=_checkout_fake()):
        assert client.post("/api/v1/billing/checkout",
                           json={"workspace_id": wid, "plan": "pro"},
                           headers=h).status_code == 409
    # pro -> enterprise via subscription.updated
    r = _hook(client, _sub_evt("evt_rv_up2", "cus_up", "sub_up", ENT_PRICE))
    assert r.status_code == 200 and r.json()["plan"] == "enterprise"
    assert repo.get_workspace(wid)["plan"] == "enterprise"
    # cancel -> free, subscription cleared, customer kept
    r = _hook(client, {"id": "evt_rv_up3", "type": "customer.subscription.deleted",
                       "data": {"object": {"id": "sub_up", "customer": "cus_up"}}})
    assert r.status_code == 200
    ws = repo.get_workspace(wid)
    assert ws["plan"] == "free" and not ws["stripe_subscription_id"]
    assert ws["stripe_customer_id"] == "cus_up"


# --- C9: unknown event + failure path ---------------------------------------

def test_C9_unknown_event_acked_200_and_recorded_processed(e2e):
    client, repo, wid, _ = e2e
    r = _hook(client, {"id": "evt_rv_unk", "type": "charge.refunded",
                       "data": {"object": {}}})
    assert r.status_code == 200 and r.json().get("ignored") is True
    assert repo.get_stripe_event("evt_rv_unk")["processed_at"] is not None


# --- C10: workspace-count gate must not depend on tie ordering (F-RV-003) ----

def test_C10_pro_owner_can_create_workspace_regardless_of_created_at_tie(e2e):
    """`POST /auth/workspaces` derived the user's plan from `owned[0]`, the
    first row of `ORDER BY w.created_at` (1-second resolution).  With a tie the
    row order is undefined, so a user who owns a PRO workspace was sometimes
    judged FREE and 403'd (flaky test_workspace_slug_uniqueness_409 in the full
    suite).  Force the tie and promote whichever workspace sorts SECOND."""
    client, repo, wid1, csrf = e2e
    h = {"X-CSRF-Token": csrf}
    repo.update_workspace(wid1, plan="pro")
    r = client.post("/auth/workspaces", json={"slug": "rv-ws-two", "name": "Two"},
                    headers=h)
    assert r.status_code in (200, 201), r.text
    wid2 = r.json()["workspace_id"]
    with repo.tx() as c:
        c.execute("UPDATE workspaces SET created_at='2026-01-01T00:00:00Z' "
                  "WHERE workspace_id IN (?,?)", (wid1, wid2))
    from auth.dependencies import list_workspaces_for_user
    uid = repo._conn().execute(
        "SELECT user_id FROM memberships WHERE workspace_id=?", (wid1,)
    ).fetchone()[0]
    order = [m["workspace_id"] for m in list_workspaces_for_user(repo, uid)]
    first, second = order[0], order[1]
    repo.update_workspace(first, plan="free")
    repo.update_workspace(second, plan="pro")     # pro workspace sorts last
    r = client.post("/auth/workspaces", json={"slug": "rv-ws-three", "name": "Three"},
                    headers=h)
    assert r.status_code in (200, 201), (
        f"owner of a PRO workspace was treated as FREE: {r.status_code} {r.text}")
