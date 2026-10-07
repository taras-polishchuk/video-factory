"""Quick probe to check whether admin role is allowed on the
billing endpoint. This is independent of the builder's tests."""
import os
import sys
from typing import Optional
from unittest.mock import patch
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


class _AuthedClient:
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
                "email": "admin_probe@example.com",
                "password": "AdminPass-1234!",
                "workspace_name": "Admin Probe Co",
            },
        )
        assert r.status_code == 200
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


def test_admin_role_can_call_billing_checkout(env):
    """Probe: does the API actually allow admin role on checkout?

    Brief criterion 3 says 'rejects admin/editor/viewer' but the
    documented intent is 'owner or admin'. Verify the code's actual
    behavior matches the documented intent.
    """
    from unittest.mock import MagicMock
    client, repo = env

    # Create a second user (signup creates its OWN workspace).
    second = client.raw.post("/auth/signup", json={
        "email": "admin_user@example.com",
        "password": "AdminUser-1234!",
        "workspace_name": "Admin User Co",
    })
    assert second.status_code == 200
    admin_uid = (
        second.json().get("user_id")
        or repo._conn().execute(
            "SELECT user_id FROM users WHERE email=?",
            ("admin_user@example.com",),
        ).fetchone()[0]
    )

    # Add the second user to the FIRST workspace as 'admin'.
    repo._conn().execute(
        "INSERT OR REPLACE INTO memberships(user_id, workspace_id, "
        "role, created_at) VALUES (?, ?, 'admin', ?)",
        (admin_uid, client.workspace_id, datetime.now(timezone.utc).isoformat()),
    )
    repo._conn().commit()

    # Login as the admin and try checkout
    raw = TestClient(client.app)
    raw.post("/auth/login", json={
        "email": "admin_user@example.com",
        "password": "AdminUser-1234!",
    })
    csrf = raw.get("/auth/csrf").json()["csrf_token"]

    fake = MagicMock()
    fake.url = "https://checkout.stripe.com/c/admin"
    fake.id = "cs_test_admin"
    fake.customer = "cus_test_admin"
    with patch("stripe.checkout.Session.create", return_value=fake):
        r = raw.post(
            "/api/v1/billing/checkout",
            json={"workspace_id": client.workspace_id, "plan": "pro"},
            headers={"X-CSRF-Token": csrf},
        )
    print(f"\nadmin role POST checkout: status={r.status_code} body={r.text[:200]}")
    # Code's actual behavior: admin IS allowed (has_min_role('admin','admin')=True)
    assert r.status_code == 200, (
        f"admin role was rejected: {r.status_code} {r.text}"
    )


def test_viewer_role_rejected(env):
    """Probe: viewer role is rejected on checkout."""
    from unittest.mock import MagicMock
    client, repo = env

    second = client.raw.post("/auth/signup", json={
        "email": "viewer_user@example.com",
        "password": "ViewerPass-1234!",
        "workspace_name": "Viewer Co",
    })
    assert second.status_code == 200
    viewer_uid = (
        second.json().get("user_id")
        or repo._conn().execute(
            "SELECT user_id FROM users WHERE email=?",
            ("viewer_user@example.com",),
        ).fetchone()[0]
    )

    repo._conn().execute(
        "INSERT OR REPLACE INTO memberships(user_id, workspace_id, "
        "role, created_at) VALUES (?, ?, 'viewer', ?)",
        (viewer_uid, client.workspace_id, datetime.now(timezone.utc).isoformat()),
    )
    repo._conn().commit()

    raw = TestClient(client.app)
    raw.post("/auth/login", json={
        "email": "viewer_user@example.com",
        "password": "ViewerPass-1234!",
    })
    csrf = raw.get("/auth/csrf").json()["csrf_token"]

    fake = MagicMock()
    fake.url = "https://checkout.stripe.com/c/x"
    fake.id = "cs_test_x"
    fake.customer = "cus_test_x"
    with patch("stripe.checkout.Session.create", return_value=fake):
        r = raw.post(
            "/api/v1/billing/checkout",
            json={"workspace_id": client.workspace_id, "plan": "pro"},
            headers={"X-CSRF-Token": csrf},
        )
    print(f"\nviewer role POST checkout: status={r.status_code} body={r.text[:200]}")
    assert r.status_code == 403, (
        f"viewer should be 403, got {r.status_code} {r.text}"
    )
