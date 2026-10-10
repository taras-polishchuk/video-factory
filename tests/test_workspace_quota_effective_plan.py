"""POST /auth/workspaces: the FREE-plan workspace limit must use the
user's *effective* plan, never "whatever row sorts first".

Regression for t_d28f7e48.  ``create_workspace`` used to take
``owned[0].plan`` where ``owned`` came from a query ordered by
``workspaces.created_at`` (1-second resolution).  Two owned workspaces
created in the same second tie, so the plan was read from a row picked
by row order: a paying user could be refused a workspace
(``403 FREE plan limit 1 workspace per user reached``), a free user
could be let through.  ``test_workspace_slug_uniqueness_409`` failed
about half of its isolated runs because of it.

The effective plan is the highest ``PLAN_RANK`` across the workspaces
the user OWNS.  These tests force the ``created_at`` tie in the DB and
run each case with the membership list in both orders, so they do not
depend on how SQLite happens to order tied rows.
"""
from __future__ import annotations

import itertools

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

TIE = "2026-01-01T00:00:00Z"
PASSWORD = "Password12345!"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VF_LOGIN_IP_WINDOW_SEC", "60")
    monkeypatch.setenv("VF_LOGIN_IP_LIMIT", "50")
    monkeypatch.setenv("VF_LOGIN_EMAIL_WINDOW_SEC", "60")
    monkeypatch.setenv("VF_LOGIN_EMAIL_LIMIT", "100")
    monkeypatch.delenv("VF_HTTPS", raising=False)
    monkeypatch.delenv("VIDEO_FACTORY_ENV", raising=False)

    import importlib
    importlib.reload(importlib.import_module("auth.ratelimit"))

    db = tmp_path / "vf.db"
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(storage))
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")

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


def _signup(client, email):
    r = client.post("/auth/signup", json={
        "email": email, "password": PASSWORD, "workspace_name": "First",
    })
    assert r.status_code == 200, r.text
    return r.json()["user_id"], r.json()["workspace_id"]


def _tie_created_at(repo, workspace_ids):
    """Give every listed workspace the SAME created_at."""
    with repo.tx() as c:
        for wid in workspace_ids:
            c.execute(
                "UPDATE workspaces SET created_at=? WHERE workspace_id=?",
                (TIE, wid),
            )


def _owned_count(repo, user_id):
    return sum(
        1 for m in repo.list_workspaces_for_user(user_id)
        if m["role"] == "owner"
    )


def _post_workspace(client, slug):
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    return client.post(
        "/auth/workspaces",
        json={"slug": slug, "name": slug.title()},
        headers={"X-CSRF-Token": csrf},
    )


@pytest.fixture(params=["as_listed", "reversed"])
def list_order(request, monkeypatch):
    """Run the endpoint with the membership list in both orders."""
    import api.auth as api_auth

    real = api_auth.list_workspaces_for_user
    if request.param == "reversed":
        monkeypatch.setattr(
            api_auth, "list_workspaces_for_user",
            lambda repo, user_id: list(reversed(real(repo, user_id))),
        )
    return request.param


# ── unit: the pick itself ────────────────────────────────────


@pytest.mark.parametrize("plans,expected", [
    ([], "free"),
    (["free"], "free"),
    (["free", "free"], "free"),
    (["pro"], "pro"),
    (["free", "pro"], "pro"),
    (["pro", "enterprise"], "enterprise"),
    (["free", "pro", "enterprise"], "enterprise"),
    # unknown / missing / odd-case plan names count as free, never
    # outrank a real paid plan
    (["gold", "free"], "free"),
    ([None, ""], "free"),
    (["gold", "pro"], "pro"),
    ([" PRO "], "pro"),
])
def test_effective_plan_is_highest_rank_in_any_order(plans, expected):
    from api.auth import _effective_plan

    for perm in itertools.permutations(plans):
        owned = [{"plan": p} for p in perm]
        assert _effective_plan(owned) == expected, perm


def test_effective_plan_tolerates_missing_plan_key():
    from api.auth import _effective_plan

    assert _effective_plan([{}, {"plan": "pro"}]) == "pro"
    assert _effective_plan([{}]) == "free"


# ── endpoint: tied created_at, mixed plans ───────────────────


@pytest.mark.parametrize("paid_plan", ["pro", "enterprise"])
@pytest.mark.parametrize("paid_is_first_row", [True, False])
def test_paid_owner_with_tied_created_at_may_create_workspace(
    env, list_order, paid_plan, paid_is_first_row,
):
    """Two owned workspaces, one paid, one free, same created_at, in
    either insertion order and either list order: creation is allowed."""
    client, repo = env
    uid, w1 = _signup(client, "paid@example.com")
    if paid_is_first_row:
        repo.update_workspace(w1, plan=paid_plan)
        w2 = repo.create_workspace(
            slug="second", name="Second", owner_user_id=uid, plan="free",
        )["workspace_id"]
    else:
        w2 = repo.create_workspace(
            slug="second", name="Second", owner_user_id=uid, plan=paid_plan,
        )["workspace_id"]
    _tie_created_at(repo, [w1, w2])
    assert _owned_count(repo, uid) == 2

    r = _post_workspace(client, "third")

    assert r.status_code == 200, r.text
    assert _owned_count(repo, uid) == 3


def test_free_user_with_two_owned_workspaces_is_still_blocked(env, list_order):
    """Tied created_at, all free: the FREE limit still applies and no
    workspace is created."""
    client, repo = env
    uid, w1 = _signup(client, "free2@example.com")
    w2 = repo.create_workspace(
        slug="second", name="Second", owner_user_id=uid, plan="free",
    )["workspace_id"]
    _tie_created_at(repo, [w1, w2])

    r = _post_workspace(client, "third")

    assert r.status_code == 403, r.text
    assert "FREE plan limit" in r.json()["detail"]
    assert _owned_count(repo, uid) == 2


def test_free_user_with_one_workspace_is_blocked(env, list_order):
    client, repo = env
    uid, _w1 = _signup(client, "free1@example.com")

    r = _post_workspace(client, "second")

    assert r.status_code == 403, r.text
    assert _owned_count(repo, uid) == 1


def test_default_free_workspace_does_not_lower_a_paid_users_plan(
    env, list_order,
):
    """New workspaces are inserted as 'free'.  A pro user who creates
    one, with every created_at tied, can keep creating more."""
    client, repo = env
    uid, w1 = _signup(client, "pro3@example.com")
    repo.update_workspace(w1, plan="pro")

    r2 = _post_workspace(client, "second")
    assert r2.status_code == 200, r2.text
    _tie_created_at(repo, [w1, r2.json()["workspace_id"]])

    r3 = _post_workspace(client, "third")
    assert r3.status_code == 200, r3.text
    assert _owned_count(repo, uid) == 3


def test_paid_workspace_owned_by_someone_else_does_not_unlock(env, list_order):
    """Only OWNED workspaces count toward the effective plan.  Being an
    editor in another user's pro workspace must not lift the FREE limit."""
    client, repo = env
    uid, w1 = _signup(client, "member@example.com")
    foreign = repo.create_workspace(
        slug="foreign-pro", name="Foreign Pro", plan="pro",
    )["workspace_id"]
    repo.add_membership(user_id=uid, workspace_id=foreign, role="editor")
    _tie_created_at(repo, [w1, foreign])

    r = _post_workspace(client, "second")

    assert r.status_code == 403, r.text
    assert _owned_count(repo, uid) == 1
