"""Regression pins: auth tokens must never be returned in HTTP responses
by default (account-takeover class, AUDIT A2 F-01 / F-04, 2026-10-09).

`POST /auth/forgot` used to return `reset_token` to any caller who knew an
email address, and `POST /auth/signup` returned `verification_token`.
Tokens are now only exposed with the explicit local-dev opt-in
`VIDEO_FACTORY_DEV_EXPOSE_TOKENS=1`, and never when
`VIDEO_FACTORY_ENV` is production/staging.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 48)
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(tmp_path / "storage"))
    monkeypatch.delenv("VIDEO_FACTORY_DEV_EXPOSE_TOKENS", raising=False)
    monkeypatch.delenv("VIDEO_FACTORY_ENV", raising=False)
    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton

    repo = Repo(str(tmp_path / "vf.db"))
    runner = JobRunner(
        repo=repo, storage_root=str(tmp_path / "storage"), schema={},
        budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
    )
    Singleton.set(runner)
    app = create_app(
        repo=repo, runner=runner, storage_root=str(tmp_path / "storage"),
        schema={},
    )
    return TestClient(app)


def _signup(client, email="victim@example.com"):
    return client.post("/auth/signup", json={
        "email": email, "password": "Password12345!",
        "workspace_name": "Victim Co",
    })


def test_forgot_does_not_return_reset_token_by_default(client):
    assert _signup(client).status_code == 200
    r = client.post("/auth/forgot", json={"email": "victim@example.com"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert "reset_token" not in r.text


def test_forgot_response_identical_for_unknown_email(client):
    assert _signup(client).status_code == 200
    known = client.post("/auth/forgot", json={"email": "victim@example.com"})
    unknown = client.post("/auth/forgot", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


def test_signup_does_not_return_verification_token_by_default(client):
    r = _signup(client)
    assert r.status_code == 200
    assert "verification_token" not in r.json()


@pytest.mark.parametrize("env", ["production", "staging"])
def test_dev_opt_in_ignored_in_production(client, monkeypatch, env):
    monkeypatch.setenv("VIDEO_FACTORY_DEV_EXPOSE_TOKENS", "1")
    monkeypatch.setenv("VIDEO_FACTORY_ENV", env)
    r = _signup(client, email=f"{env}@example.com")
    assert r.status_code == 200
    assert "verification_token" not in r.json()
    r2 = client.post("/auth/forgot", json={"email": f"{env}@example.com"})
    assert "reset_token" not in r2.json()


def test_dev_opt_in_still_supports_local_e2e(client, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_DEV_EXPOSE_TOKENS", "1")
    r = _signup(client, email="dev@example.com")
    assert r.json().get("verification_token")
    r2 = client.post("/auth/forgot", json={"email": "dev@example.com"})
    assert r2.json().get("reset_token")
