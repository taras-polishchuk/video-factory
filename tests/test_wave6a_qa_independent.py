"""Wave 6a QA — independent adversarial tests (Wave 6a QA follow-up).

These tests were written by the QA profile (independent of the
builder) to verify the brief's "real-shape Checkout Session +
failure-path handling" requirements that the builder's own suite
did NOT cover.

Two defect classes targeted:

F-W6A-001 — Stripe API error on checkout is NOT translated to 502
            with a `billing.checkout_failed` audit row. The brief
            requires:
                "Failure paths (Stripe API error 4xx/5xx) surface as
                 502 Bad Gateway with audit row `billing.checkout_failed`"
            The current `api/main.py` lets `StripeCallError` propagate
            to FastAPI's default 500 handler and writes no audit
            row.

F-W6A-002 — Stripe API error on the portal endpoint has the same
            defect: 500 + no `billing.portal_failed` audit row.

These tests are designed to FAIL on the current code. They will
turn green when the dev closes F-W6A-001 / F-W6A-002.
"""
from __future__ import annotations

import os
import sys
from typing import Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ── Reuse the builder's _AuthedClient + billing_env fixture ───


def _checkout_response(url="https://checkout.stripe.com/c/test",
                          session_id="cs_test_abc",
                          customer_id="cus_test_xyz"):
    from unittest.mock import MagicMock
    sess = MagicMock()
    sess.url = url
    sess.id = session_id
    sess.customer = customer_id
    return sess


def _portal_response(url="https://billing.stripe.com/p/test",
                       session_id="bps_test_abc"):
    from unittest.mock import MagicMock
    sess = MagicMock()
    sess.url = url
    sess.id = session_id
    return sess


class _AuthedClient:
    """Local copy of the builder's _AuthedClient so this file is
    self-contained (does not import from the builder's
    test_wave6_billing.py)."""
    _STATEFUL = ("post", "put", "patch", "delete")

    def __init__(self, raw):
        self._raw = raw
        self._csrf = None
        self._signed_up = False
        self._workspace_id: Optional[str] = None
        self._user_id: Optional[str] = None

    def _ensure_signed_up(self):
        if self._signed_up:
            return
        r = self._raw.post(
            "/auth/signup",
            json={
                "email": "qa6a@example.com",
                "password": "Qa6aPass-1234!",
                "workspace_name": "QA6a Co",
            },
        )
        assert r.status_code == 200, (
            f"signup failed: {r.status_code} {r.text}"
        )
        body = r.json()
        self._user_id = body.get("user_id")
        self._workspace_id = body.get("workspace_id") or body.get(
            "company_id"
        )
        self._signed_up = True

    def _ensure_csrf(self):
        if not self._csrf:
            self._ensure_signed_up()
            r = self._raw.get("/auth/csrf")
            assert r.status_code == 200
            self._csrf = r.json()["csrf_token"]
        return self._csrf

    def request(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        if method.lower() in self._STATEFUL:
            headers.setdefault("X-CSRF-Token", self._ensure_csrf())
        return self._raw.request(method, url, headers=headers, **kwargs)

    def get(self, url, **kwargs):
        return self._raw.get(url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    @property
    def workspace_id(self):
        if not self._signed_up:
            self._ensure_signed_up()
        return self._workspace_id

    @property
    def user_id(self):
        if not self._signed_up:
            self._ensure_signed_up()
        return self._user_id

    @property
    def raw(self):
        return self._raw

    @property
    def app(self):
        return self._raw.app


@pytest.fixture()
def billing_env(tmp_path, monkeypatch):
    """Same fixture as the builder's, isolated per test."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(tmp_path / "storage"))
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_env")
    monkeypatch.setenv("STRIPE_PRO_PRICE_ID", "price_pro_test")

    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton

    repo = Repo(str(tmp_path / "vf.db"))
    runner = JobRunner(
        repo=repo,
        storage_root=str(tmp_path / "storage"),
        schema={},
        budget_per_video_usd=5.0,
        budget_per_batch_usd=20.0,
    )
    Singleton.set(runner)
    app = create_app(
        repo=repo, runner=runner,
        storage_root=str(tmp_path / "storage"),
        schema={},
    )
    client = _AuthedClient(TestClient(app))
    from api import billing as _billing
    _billing.reset_stripe_client()
    yield client, repo
    _billing.reset_stripe_client()


# ── F-W6A-001: Stripe API error -> 502 + billing.checkout_failed audit ──


def test_f_w6a_001_checkout_stripe_error_surfaces_502_with_audit(
        billing_env):
    """Brief requirement: "Failure paths (Stripe API error 4xx/5xx)
    surface as 502 Bad Gateway with audit row `billing.checkout_failed`".

    Current code lets the wrapper's StripeCallError propagate to
    FastAPI's default 500 handler and writes no audit row.
    """
    from stripe._error import StripeError
    client, repo = billing_env

    # Simulate Stripe returning a 4xx (e.g. invalid API key).
    fake_error = StripeError("invalid API key")
    with patch("stripe.checkout.Session.create",
                side_effect=fake_error):
        r = client.post("/api/v1/billing/checkout", json={
            "workspace_id": client.workspace_id,
            "plan": "pro",
        })
    # 1) HTTP status must be 502 (upstream error), NOT 500.
    assert r.status_code == 502, (
        f"expected 502 Bad Gateway, got {r.status_code}: {r.text}"
    )
    # 2) Response body must explain the upstream failure
    body = r.json()
    assert "stripe" in (body.get("error", "") or "").lower() or \
           "checkout" in (body.get("error", "") or "").lower(), (
        f"expected stripe/checkout error message, got: {body}"
    )
    # 3) Audit row billing.checkout_failed must exist for the workspace
    audit_rows = repo.list_audit(workspace_id=client.workspace_id, limit=20)
    events = [row["event"] for row in audit_rows]
    assert "billing.checkout_failed" in events, (
        f"expected billing.checkout_failed audit row, "
        f"events found: {events}"
    )
    fail_row = next(
        row for row in audit_rows
        if row["event"] == "billing.checkout_failed"
    )
    # 4) Audit context must NOT leak Stripe customer/subscription IDs
    assert "stripe_customer_id" not in fail_row["context"]
    assert "stripe_subscription_id" not in fail_row["context"]
    # 5) Audit context must record plan + workspace for diagnostics
    assert fail_row["context"]["plan"] == "pro"
    assert fail_row["context"]["workspace_id"] == client.workspace_id


def test_f_w6a_001b_checkout_stripe_5xx_error_surfaces_502_with_audit(
        billing_env):
    """Same as F-W6A-001 but with a 5xx-shaped error (Stripe service
    unavailable) — must still be 502 + audit row, NOT 500."""
    from stripe._error import APIConnectionError
    client, repo = billing_env

    fake_error = APIConnectionError("stripe unreachable")
    with patch("stripe.checkout.Session.create",
                side_effect=fake_error):
        r = client.post("/api/v1/billing/checkout", json={
            "workspace_id": client.workspace_id,
            "plan": "pro",
        })
    assert r.status_code == 502, (
        f"expected 502 for Stripe 5xx, got {r.status_code}: {r.text}"
    )
    audit_rows = repo.list_audit(workspace_id=client.workspace_id, limit=20)
    events = [row["event"] for row in audit_rows]
    assert "billing.checkout_failed" in events


# ── F-W6A-002: portal error -> 502 + billing.portal_failed audit ──


def test_f_w6a_002_portal_stripe_error_surfaces_502_with_audit(
        billing_env):
    """Brief requires the SAME failure-path handling for the
    Customer Portal endpoint. The portal endpoint is gated on
    stripe_customer_id, so we seed one first, then patch the
    portal SDK to raise."""
    from stripe._error import StripeError
    client, repo = billing_env

    # Seed a customer so the portal endpoint gets past the 404 gate.
    fake_checkout = _checkout_response(customer_id="cus_seeded_for_portal")
    with patch("stripe.checkout.Session.create",
                return_value=fake_checkout):
        r = client.post("/api/v1/billing/checkout", json={
            "workspace_id": client.workspace_id,
            "plan": "pro",
        })
    assert r.status_code == 200, r.text

    # Now patch the portal SDK to fail.
    fake_error = StripeError("portal API error")
    with patch("stripe.billing_portal.Session.create",
                side_effect=fake_error):
        r = client.post("/api/v1/billing/portal", json={
            "workspace_id": client.workspace_id,
        })
    assert r.status_code == 502, (
        f"expected 502 for portal Stripe error, got {r.status_code}: "
        f"{r.text}"
    )
    audit_rows = repo.list_audit(workspace_id=client.workspace_id, limit=20)
    events = [row["event"] for row in audit_rows]
    assert "billing.portal_failed" in events, (
        f"expected billing.portal_failed audit row, events: {events}"
    )
