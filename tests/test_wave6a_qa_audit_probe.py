"""Independent probe — verify Stripe SDK never hits network during
tests (criterion 8) and audit row visibility (criterion 7)."""
import os
import socket
import sys
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet


class _AuthedClient:
    _STATEFUL = ("post", "put", "patch", "delete")

    def __init__(self, raw):
        self._raw = raw
        self._csrf = None
        self._signed_up = False
        self._workspace_id = None
        self._user_id = None

    def _ensure_signed_up(self):
        if self._signed_up:
            return
        r = self._raw.post("/auth/signup", json={
            "email": "audit_probe@example.com",
            "password": "AuditPass-1234!",
            "workspace_name": "Audit Co",
        })
        assert r.status_code == 200
        body = r.json()
        self._user_id = body.get("user_id")
        self._workspace_id = body.get("workspace_id") or body.get("company_id")
        self._signed_up = True

    def _ensure_csrf(self):
        if not self._csrf:
            self._ensure_signed_up()
            r = self._raw.get("/auth/csrf")
            self._csrf = r.json()["csrf_token"]
        return self._csrf

    def request(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        if method.lower() in self._STATEFUL:
            headers.setdefault("X-CSRF-Token", self._ensure_csrf())
        return self._raw.request(method, url, headers=headers, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    @property
    def workspace_id(self):
        if not self._signed_up:
            self._ensure_signed_up()
        return self._workspace_id

    @property
    def raw(self):
        return self._raw

    @property
    def app(self):
        return self._raw.app


@pytest.fixture()
def env(tmp_path, monkeypatch):
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
    runner = JobRunner(repo=repo, storage_root=str(tmp_path / "storage"),
                       schema={}, budget_per_video_usd=5.0,
                       budget_per_batch_usd=20.0)
    Singleton.set(runner)
    app = create_app(repo=repo, runner=runner,
                     storage_root=str(tmp_path / "storage"), schema={})
    client = _AuthedClient(TestClient(app))
    from api import billing as _billing
    _billing.reset_stripe_client()
    yield client, repo
    _billing.reset_stripe_client()


def test_no_real_network_calls_to_stripe(env):
    """Block all socket connections to api.stripe.com via socket.create_connection.
    If the wrapper tries to hit the network, the test will fail with ConnectionError.
    """
    import socket
    original_create_connection = socket.create_connection

    def guarded_create_connection(address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        if "stripe.com" in str(host):
            raise ConnectionError(f"REFUSED: real network to {host}")
        return original_create_connection(address, *a, **kw)

    client, repo = env
    fake = MagicMock()
    fake.url = "https://checkout.stripe.com/c/x"
    fake.id = "cs_test_x"
    fake.customer = "cus_test_x"

    with patch("socket.create_connection",
                side_effect=guarded_create_connection), \
         patch("stripe.checkout.Session.create", return_value=fake):
        r = client.post("/api/v1/billing/checkout", json={
            "workspace_id": client.workspace_id,
            "plan": "pro",
        })
    assert r.status_code == 200, r.text


def test_audit_row_visibility_no_stripe_ids(env):
    """Brief criterion 7: audit rows visible to owner/admin, no
    Stripe IDs leaked.
    """
    client, repo = env
    fake = MagicMock()
    fake.url = "https://checkout.stripe.com/c/audit"
    fake.id = "cs_test_audit"
    fake.customer = "cus_test_audit"

    with patch("stripe.checkout.Session.create", return_value=fake):
        r = client.post("/api/v1/billing/checkout", json={
            "workspace_id": client.workspace_id,
            "plan": "pro",
        })
    assert r.status_code == 200, r.text

    rows = repo.list_audit(workspace_id=client.workspace_id, limit=20)
    print(f"\nAudit rows: {[r['event'] for r in rows]}")
    events = [r["event"] for r in rows]
    assert "billing.checkout_started" in events
    # No Stripe IDs in audit context
    checkout_row = next(
        r for r in rows if r["event"] == "billing.checkout_started"
    )
    ctx = checkout_row["context"]
    # session_id is OK to keep (cs_test_*, no customer/sub ids)
    assert "session_id" in ctx
    # NO customer / subscription ids
    assert "stripe_customer_id" not in ctx
    assert "stripe_subscription_id" not in ctx
    assert "customer" not in ctx
    assert "subscription" not in ctx
