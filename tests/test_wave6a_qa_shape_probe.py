"""Independent probe — real-shape Checkout Session params.

Brief: assert mode='subscription', line_items referencing Pro plan
price, success_url, cancel_url, client_reference_id=workspace_id,
customer_email from workspace owner.
"""
from unittest.mock import MagicMock, patch
import os
from cryptography.fernet import Fernet
import pytest
from fastapi.testclient import TestClient


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
            "email": "shape_probe@example.com",
            "password": "ShapePass-1234!",
            "workspace_name": "Shape Co",
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


def test_real_shape_checkout_session(env):
    """Brief: real-shape Checkout Session with mode='subscription',
    line_items, success_url, cancel_url, client_reference_id=workspace_id,
    customer_email from workspace owner.
    """
    client, _ = env
    fake = MagicMock()
    fake.url = "https://checkout.stripe.com/c/test"
    fake.id = "cs_test_abc"
    fake.customer = "cus_test_xyz"

    with patch("stripe.checkout.Session.create", return_value=fake) as mc:
        r = client.post("/api/v1/billing/checkout", json={
            "workspace_id": client.workspace_id,
            "plan": "pro",
        })
    assert r.status_code == 200, r.text

    kwargs = mc.call_args.kwargs
    print(f"\nCheckout params: {kwargs}")
    # Brief requirements
    assert kwargs.get("mode") == "subscription"
    assert kwargs.get("line_items") == [{"price": "price_pro_test",
                                          "quantity": 1}]
    assert "/billing/success" in kwargs.get("success_url", "")
    assert "/billing/cancel" in kwargs.get("cancel_url", "")
    assert kwargs.get("client_reference_id") == client.workspace_id
    # Brief: customer_email from workspace owner
    # The workspace owner signed up with email 'shape_probe@example.com'
    assert kwargs.get("customer_email") == "shape_probe@example.com", (
        f"customer_email must be the workspace owner's email, "
        f"got {kwargs.get('customer_email')!r}"
    )
