"""End-to-end API tests for Video Factory Control Room.

Uses FastAPI TestClient with isolated DB + storage per test.
"""
from __future__ import annotations

import io
import os
import pathlib
import shutil

import pytest
from fastapi.testclient import TestClient


class _AuthedClient:
    """A TestClient that signs up a user on first use and
    automatically attaches the X-CSRF-Token header to every
    state-changing request (POST / PUT / PATCH / DELETE).

    Wave 2 follow-up (t_d76b7c0c): the legacy alias write
    paths now require auth + CSRF + membership. Existing
    tests in this file predate the fix; this wrapper
    preserves their wire-level expectations (200 on POST,
    no headers in the call site) while exercising the new
    auth path.
    """

    _STATEFUL = ("post", "put", "patch", "delete")

    def __init__(self, raw: TestClient):
        self._raw = raw
        self._csrf = None
        self._signed_up = False

    def _ensure_signed_up(self) -> None:
        if self._signed_up:
            return
        r = self._raw.post(
            "/auth/signup",
            json={
                "email": "tester@video-factory.test",
                "password": "TestPass-1234!",
                "workspace_name": "Test Co",
            },
        )
        assert r.status_code == 200, (
            f"authed_client: signup failed {r.status_code}: {r.text}"
        )
        self._signed_up = True

    def _ensure_csrf(self) -> str:
        if not self._csrf:
            self._ensure_signed_up()
            r = self._raw.get("/auth/csrf")
            assert r.status_code == 200, (
                f"authed_client: csrf fetch failed {r.status_code}: {r.text}"
            )
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

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    @property
    def cookies(self):
        return self._raw.cookies

    @property
    def app(self):
        return self._raw.app

    @property
    def raw(self):
        """Underlying un-authed TestClient.

        Used by tests that need to assert the auth gate (i.e.
        explicitly want a request with NO session cookie).
        """
        return self._raw


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "vf.db"
    storage = tmp_path / "storage"
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(storage))
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")
    # Auth endpoints 503 without VIDEO_FACTORY_SESSION_SECRET.
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    # Force reimport so env vars take effect on create_app defaults.
    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton

    repo = Repo(str(db))
    runner = JobRunner(
        repo=repo,
        storage_root=str(storage),
        schema={},
        budget_per_video_usd=5.0,
        budget_per_batch_usd=20.0,
    )
    Singleton.set(runner)
    app = create_app(repo=repo, runner=runner, storage_root=str(storage), schema={})
    # Yield an auto-authed client so legacy tests that did not
    # sign up still get a session + CSRF on write paths.
    yield _AuthedClient(TestClient(app)), repo, storage


def _wait_done(client, job_id, timeout_s=60):
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        j = client.get(f"/api/v1/jobs/{job_id}").json()
        if j["state"] in ("done", "failed"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"job did not finish in {timeout_s}s")


def test_health(env):
    client, _, _ = env
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["mock_only"] is True
    assert "version" in body


def test_company_crud(env):
    client, _, _ = env
    r = client.post("/api/v1/companies", json={"slug": "acme", "name": "Acme"})
    assert r.status_code == 200
    cid = r.json()["company_id"]
    r = client.get(f"/api/v1/companies/{cid}")
    assert r.status_code == 200
    r = client.patch(f"/api/v1/companies/{cid}", json={"description": "hi"})
    assert r.json()["description"] == "hi"


def test_bible_draft_then_approve(env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    r = client.post(f"/api/v1/companies/{cid}/bibles", json={"payload": {"name": "A", "audience": "x"}})
    bid = r.json()["bible_id"]
    assert r.json()["status"] == "draft"
    r = client.post(f"/api/v1/bibles/{bid}/approve")
    assert r.json()["status"] == "approved"


def test_bible_version_increments(env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    b1 = client.post(f"/api/v1/companies/{cid}/bibles", json={"payload": {"name": "A"}}).json()
    b2 = client.post(f"/api/v1/companies/{cid}/bibles", json={"payload": {"name": "A", "audience": "y"}}).json()
    assert b2["version"] == b1["version"] + 1


def test_cross_company_bible_denied(env):
    client, _, _ = env
    c1 = client.post("/api/v1/companies", json={"slug": "c1", "name": "C1"}).json()["company_id"]
    c2 = client.post("/api/v1/companies", json={"slug": "c2", "name": "C2"}).json()["company_id"]
    bid = client.post(f"/api/v1/companies/{c1}/bibles", json={"payload": {"name": "C1"}}).json()["bible_id"]
    client.post(f"/api/v1/bibles/{bid}/approve")
    r = client.post("/api/v1/jobs", json={
        "company_id": c2, "bible_id": bid, "topic": "steal",
        "language": "en-US", "duration_seconds": 10, "platforms": ["tiktok"],
    })
    assert r.status_code == 403


def test_job_unapproved_bible_denied(env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    bid = client.post(f"/api/v1/companies/{cid}/bibles", json={"payload": {"name": "A"}}).json()["bible_id"]
    r = client.post("/api/v1/jobs", json={
        "company_id": cid, "bible_id": bid, "topic": "x",
        "language": "en-US", "duration_seconds": 10, "platforms": ["tiktok"],
    })
    assert r.status_code == 400


def test_full_mock_job_to_done(env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    bid = client.post(f"/api/v1/companies/{cid}/bibles",
                       json={"payload": {"name": "A", "audience": "devs"}}).json()["bible_id"]
    client.post(f"/api/v1/bibles/{bid}/approve")
    jid = client.post("/api/v1/jobs", json={
        "company_id": cid, "bible_id": bid, "topic": "Hello world",
        "language": "en-US", "duration_seconds": 12, "platforms": ["tiktok"],
    }).json()["job_id"]
    final = _wait_done(client, jid)
    assert final["state"] == "done", final["status_message"]
    stages = client.get(f"/api/v1/jobs/{jid}/stages").json()
    assert len(stages) == 12
    for s in stages:
        assert s["status"] == "done", f"stage {s['stage_key']} not done: {s}"
    outs = client.get(f"/api/v1/jobs/{jid}/outputs").json()
    final_outs = [o for o in outs if o["kind"] == "final_video"]
    assert len(final_outs) >= 1
    o = final_outs[0]
    assert o["size_bytes"] > 1000, "final mp4 must be >1KB"


def test_final_video_downloads_as_real_mp4(env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    bid = client.post(f"/api/v1/companies/{cid}/bibles",
                       json={"payload": {"name": "A", "audience": "x"}}).json()["bible_id"]
    client.post(f"/api/v1/bibles/{bid}/approve")
    jid = client.post("/api/v1/jobs", json={
        "company_id": cid, "bible_id": bid, "topic": "test",
        "language": "en-US", "duration_seconds": 9, "platforms": ["tiktok"],
    }).json()["job_id"]
    _wait_done(client, jid)
    outs = client.get(f"/api/v1/jobs/{jid}/outputs").json()
    final = next(o for o in outs if o["kind"] == "final_video")
    r = client.get(f"/api/v1/jobs/{jid}/outputs/{final['output_id']}/download")
    assert r.status_code == 200
    # Real h264 mp4 starts with 00 00 00 (ftyp box)
    assert r.content[:3] == b"\x00\x00\x00", "expected MP4 ftyp box"


def test_publishing_intents(env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    bid = client.post(f"/api/v1/companies/{cid}/bibles",
                       json={"payload": {"name": "A", "audience": "x"}}).json()["bible_id"]
    client.post(f"/api/v1/bibles/{bid}/approve")
    jid = client.post("/api/v1/jobs", json={
        "company_id": cid, "bible_id": bid, "topic": "t",
        "language": "en-US", "duration_seconds": 6, "platforms": ["tiktok"],
    }).json()["job_id"]
    _wait_done(client, jid)
    for action, body in [
        ("draft", {"action": "draft", "platforms": ["tiktok"]}),
        ("schedule", {"action": "schedule", "platforms": ["ig"], "scheduled_for": "2026-10-05T09:00:00Z"}),
        ("publish", {"action": "publish", "platforms": ["tiktok", "linkedin"]}),
    ]:
        r = client.post(f"/api/v1/jobs/{jid}/publish", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "done"
    intents = client.get(f"/api/v1/jobs/{jid}/publish").json()
    assert len(intents) == 3


def test_publish_requires_done_state(env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    bid = client.post(f"/api/v1/companies/{cid}/bibles",
                       json={"payload": {"name": "A", "audience": "x"}}).json()["bible_id"]
    client.post(f"/api/v1/bibles/{bid}/approve")
    jid = client.post("/api/v1/jobs", json={
        "company_id": cid, "bible_id": bid, "topic": "t",
        "language": "en-US", "duration_seconds": 6, "platforms": ["tiktok"],
    }).json()["job_id"]
    r = client.post(f"/api/v1/jobs/{jid}/publish", json={"action": "draft", "platforms": ["tiktok"]})
    assert r.status_code == 400


def test_asset_upload_and_download(env):
    client, _, storage = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    files = {"file": ("logo.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"X" * 200), "image/png")}
    r = client.post(f"/api/v1/companies/{cid}/assets?kind=logo", files=files)
    assert r.status_code == 200
    aid = r.json()["asset_id"]
    r = client.get(f"/api/v1/assets/{aid}/download")
    assert r.status_code == 200
    assert len(r.content) == 200 + 8


def test_asset_path_traversal_blocked(env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    files = {"file": ("../escape.txt", io.BytesIO(b"x" * 10), "text/plain")}
    r = client.post(f"/api/v1/companies/{cid}/assets?kind=misc", files=files)
    assert r.status_code == 400


def test_integrations_are_disabled(env):
    client, _, _ = env
    r = client.get("/api/v1/integrations")
    items = r.json()
    assert all(item["enabled"] is False for item in items)
    names = {item["name"] for item in items}
    assert {"heygen", "comfy_cloud", "comfy_gpu_worker", "n8n", "publer"} <= names


def test_job_list_filters(env):
    client, _, _ = env
    c1 = client.post("/api/v1/companies", json={"slug": "c1", "name": "C1"}).json()["company_id"]
    c2 = client.post("/api/v1/companies", json={"slug": "c2", "name": "C2"}).json()["company_id"]
    b1 = client.post(f"/api/v1/companies/{c1}/bibles",
                      json={"payload": {"name": "C1", "audience": "x"}}).json()["bible_id"]
    b2 = client.post(f"/api/v1/companies/{c2}/bibles",
                      json={"payload": {"name": "C2", "audience": "x"}}).json()["bible_id"]
    client.post(f"/api/v1/bibles/{b1}/approve")
    client.post(f"/api/v1/bibles/{b2}/approve")
    client.post("/api/v1/jobs", json={
        "company_id": c1, "bible_id": b1, "topic": "a",
        "language": "en-US", "duration_seconds": 6, "platforms": ["tiktok"],
    })
    client.post("/api/v1/jobs", json={
        "company_id": c2, "bible_id": b2, "topic": "b",
        "language": "en-US", "duration_seconds": 6, "platforms": ["tiktok"],
    })
    r = client.get(f"/api/v1/jobs?company_id={c1}")
    assert all(j["company_id"] == c1 for j in r.json())
    assert len(r.json()) == 1