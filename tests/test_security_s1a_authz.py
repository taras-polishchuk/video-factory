"""SECURITY S1a: authorization on every tenant route + file integrity.

Source findings: A1 (t_7fd75e64) F1, F2, F3, F4, F10, F13, F16, F17
and A2 (t_ff380346) F-17.

What this file pins down:

* ``test_route_walk_*`` walk ``app.routes``.  Any route that is not
  in ``PUBLIC_ROUTES`` must answer 401 to an anonymous caller, and
  any state-changing route must answer 403 to a logged-in caller
  that does not send ``X-CSRF-Token``.  A new route added without
  auth fails here until it is protected or explicitly allow-listed
  with a reason.
* ``TENANT_ROUTES`` is the table of resource-id keyed tenant routes.
  For each: anonymous -> 401, member of another workspace -> 404,
  own member -> 2xx.
* Downloads: forged / missing / foreign HMAC sidecar -> 404, path
  escape -> 404, ``nosniff`` + ``attachment`` on success.
* Uploads: type not allowed for the kind -> 415, bytes that do not
  match the declared type -> 415, over the cap -> 413.
* ``/health`` reports ``mock_only`` from the real live gate.

Users and sessions are seeded through the repo (not /auth/signup)
so this file does not depend on the signup/CSRF rules owned by the
S1b card.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


# ── public allow-list ────────────────────────────────────────────
# (METHOD, path template) -> why it may be reached without a session.
# Every entry must leak no tenant data; test_public_routes_leak_no_
# tenant_data checks the GET ones against seeded tenant identifiers.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/health"): "liveness + version + ffmpeg flag + mock_only gate",
    ("GET", "/healthz"): "k8s liveness, constant body",
    ("GET", "/readyz"): "k8s readiness, subsystem ok/error strings only",
    ("GET", "/metrics"): "Prometheus; labels are route templates, no ids",
    ("GET", "/api/v1/spec"): "static pointer to /openapi.json",
    ("GET", "/api/v1/schemas/bible"): "static JSON schema file",
    ("GET", "/openapi.json"): "FastAPI schema, no data",
    ("GET", "/docs"): "Swagger UI shell",
    ("GET", "/docs/oauth2-redirect"): "Swagger UI helper",
    ("GET", "/redoc"): "ReDoc shell",
    # Pre-session auth flows (CSRF/origin rules owned by S1b).
    ("POST", "/auth/signup"): "creates the session",
    ("POST", "/auth/login"): "creates the session",
    ("POST", "/auth/forgot"): "pre-session; generic ok body",
    ("POST", "/auth/forgot/consume"): "pre-session; token in body",
    ("POST", "/auth/verify"): "pre-session; token in body",
    ("GET", "/auth/oauth/{provider}/start"): "OIDC redirect start",
    ("GET", "/auth/oauth/{provider}/callback"): "OIDC redirect target",
    # Signed by the provider, verified in the handler.
    ("POST", "/api/v1/webhooks/heygen"): "HMAC signature verified",
    ("POST", "/api/v1/webhooks/stripe"): "Stripe signature verified",
    # Only mounted when the router failed to build (A2 F-20).
    ("GET", "/_stripe_init_error"): "config error string, no tenant data",
    ("GET", "/_oidc_init_error"): "config error string, no tenant data",
}

# State-changing routes with a known CSRF gap owned by another card.
CSRF_KNOWN_GAPS: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/admin/keys/rotate"):
        "A1 F11: CSRF + re-auth on key rotation is owned by SECURITY S1b",
}

_STATEFUL = {"POST", "PUT", "PATCH", "DELETE"}
_PARAM_VALUES = {"provider": "google"}


# ── fixtures ─────────────────────────────────────────────────────

@pytest.fixture()
def s1a(tmp_path, monkeypatch):
    db = tmp_path / "vf.db"
    storage = tmp_path / "storage"
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(storage))
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "s" * 64)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY",
                       Fernet.generate_key().decode())
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "s1a-dummy-not-a-secret")
    monkeypatch.delenv("VIDEO_FACTORY_DB_KEY", raising=False)
    monkeypatch.delenv("LIVE_PROVIDER_TESTS", raising=False)
    monkeypatch.delenv("SMOKE_TEST_PASSED", raising=False)
    monkeypatch.delenv("VIDEO_FACTORY_MAX_UPLOAD_MB", raising=False)

    from api.main import create_app
    from control_plane import artifact_hmac
    from core.repo import Repo
    from core.runners import JobRunner, Singleton

    repo = Repo(str(db))
    runner = JobRunner(repo=repo, storage_root=str(storage), schema={},
                       budget_per_video_usd=5.0, budget_per_batch_usd=20.0)
    submitted: list[str] = []
    # No background rendering in this file: jobs stay where the test
    # puts them, and no ffmpeg is spawned.
    runner.submit = lambda job_id: submitted.append(job_id)
    Singleton.set(runner)
    app = create_app(repo=repo, runner=runner, storage_root=str(storage),
                     schema={})

    def user(email: str, slug: str | None = None):
        from auth.session import (derive_csrf_token, expires_at_iso,
                                  new_session_id)
        u = repo.create_user(email=email, password_hash="unused")
        wid = None
        if slug:
            wid = repo.create_workspace(slug=slug, name=slug.upper(),
                                        owner_user_id=u["user_id"])[
                "workspace_id"]
        sid = new_session_id()
        csrf = derive_csrf_token(sid)
        repo.create_session(user_id=u["user_id"], csrf_token=csrf, ip=None,
                            user_agent=None, expires_at=expires_at_iso(),
                            session_id=sid)
        client = TestClient(app)
        client.cookies.set("vf_session", sid)
        return SimpleNamespace(client=client, csrf=csrf,
                               hdr={"X-CSRF-Token": csrf},
                               user_id=u["user_id"], wid=wid, email=email)

    def blob(wid: str, key: str, data: bytes, *, sidecar_wid: str | None = "same"):
        p = storage / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if sidecar_wid is not None:
            artifact_hmac.write_sidecar(
                storage_root=str(storage), storage_key=key,
                workspace_id=wid if sidecar_wid == "same" else sidecar_wid,
            )
        return key

    alice = user("alice@s1a.test", "alice-ws")
    bob = user("bob@s1a.test", "bob-ws")
    vic = user("vic@s1a.test")  # viewer in Alice's workspace
    repo.add_membership(user_id=vic.user_id, workspace_id=alice.wid,
                        role="viewer")
    vic.wid = alice.wid

    bible = repo.create_bible(workspace_id=alice.wid,
                              payload={"name": "A", "audience": "x"})
    repo.approve_bible(bible["bible_id"])
    bible_draft = repo.create_bible(workspace_id=alice.wid,
                                    payload={"name": "A2"})
    job = repo.create_job(workspace_id=alice.wid, topic="alice secret topic",
                          language="en-US", duration_seconds=6,
                          platforms=["tiktok"])
    repo.update_job(job["job_id"], state="failed")
    job_done = repo.create_job(workspace_id=alice.wid, topic="alice done",
                               language="en-US", duration_seconds=6,
                               platforms=["tiktok"])
    repo.update_job(job_done["job_id"], state="done")
    out_key = blob(alice.wid, f"jobs/{job_done['job_id']}/final/f.mp4",
                   b"\x00\x00\x00\x18ftypmp42" + b"A" * 64)
    output = repo.create_output(job_done["job_id"], storage_key=out_key,
                                filename="f.mp4", mime_type="video/mp4",
                                size_bytes=76, checksum="0" * 64,
                                kind="final_video")
    asset_key = blob(alice.wid, "workspaces/alice-ws/assets/ast_x_logo.png",
                     b"\x89PNG\r\n\x1a\n" + b"P" * 32)
    asset = repo.create_asset(workspace_id=alice.wid, kind="logo",
                              filename="logo.png", storage_key=asset_key,
                              mime_type="image/png", size_bytes=40,
                              checksum="0" * 64)
    bob_job = repo.create_job(workspace_id=bob.wid, topic="bob secret topic",
                              language="en-US", duration_seconds=6,
                              platforms=["tiktok"])
    bob_key = blob(bob.wid, "workspaces/bob-ws/assets/ast_b_secret.pdf",
                   b"%PDF-1.4 bob private")
    bob_asset = repo.create_asset(workspace_id=bob.wid, kind="doc",
                                  filename="secret.pdf", storage_key=bob_key,
                                  mime_type="application/pdf",
                                  size_bytes=20, checksum="0" * 64)

    ids = {
        "wid": alice.wid,
        "job": job["job_id"],
        "job_done": job_done["job_id"],
        "output": output["output_id"],
        "asset": asset["asset_id"],
        "bible": bible["bible_id"],
        "bible_draft": bible_draft["bible_id"],
    }
    return SimpleNamespace(app=app, repo=repo, storage=storage,
                           anon=TestClient(app), alice=alice, bob=bob,
                           vic=vic, ids=ids, submitted=submitted,
                           bob_job=bob_job, bob_asset=bob_asset, blob=blob)


def _body_kwargs(route) -> dict:
    """A plausible body so the handler (not body validation) runs."""
    if not isinstance(route, APIRoute):
        return {}
    params = route.dependant.body_params
    if not params:
        return {}
    for p in params:
        ann = getattr(p.field_info, "annotation", None) or getattr(p, "type_", None)
        if "UploadFile" in repr(ann) or "File" in type(p.field_info).__name__:
            return {"files": {"file": ("probe.png",
                                       io.BytesIO(b"\x89PNG\r\n\x1a\nxx"),
                                       "image/png")}}
    return {"json": {}}


def _concrete(path: str, values: dict | None = None) -> str:
    vals = {**_PARAM_VALUES, **(values or {})}
    return re.sub(r"\{(\w+)(:[^}]*)?\}",
                  lambda m: vals.get(m.group(1), "probe_x"), path)


def _flat_routes(app):
    """(path, route) for every HTTP route, including routes inside
    included routers.  FastAPI >= 0.14x keeps included routers as
    ``_IncludedRouter`` nodes instead of copying their routes into
    ``app.routes``; older versions flatten them.  Handle both.
    """
    for r in app.routes:
        contexts = getattr(r, "effective_route_contexts", None)
        if callable(contexts):
            for ctx in contexts():
                orig = ctx.original_route
                yield (getattr(ctx, "path", "") or orig.path), orig
        else:
            yield r.path, r


def _app_routes(app):
    for path, r in _flat_routes(app):
        methods = getattr(r, "methods", None)
        if not methods:
            continue
        for method in sorted(set(methods) - {"HEAD", "OPTIONS"}):
            yield method, path, r


# ── route walk ───────────────────────────────────────────────────

def test_route_walk_every_non_public_route_rejects_anonymous(s1a):
    seen_public = set()
    failures = []
    checked = 0
    for method, path, route in _app_routes(s1a.app):
        key = (method, path)
        if key in PUBLIC_ROUTES:
            seen_public.add(key)
            continue
        r = s1a.anon.request(method, _concrete(path), **_body_kwargs(route))
        checked += 1
        if r.status_code != 401:
            failures.append(f"{method} {path} -> {r.status_code}")
    assert not failures, (
        "routes reachable without a session (protect them or add to "
        "PUBLIC_ROUTES with a reason):\n" + "\n".join(failures)
    )
    # Sanity: the walk really covered the tenant surface.
    assert checked >= 40, checked
    assert ("GET", "/health") in seen_public
    assert ("POST", "/auth/login") in seen_public


@pytest.fixture()
def stateful_routes(s1a):
    return {(method, path): route
            for method, path, route in _app_routes(s1a.app)
            if method in _STATEFUL and (method, path) not in PUBLIC_ROUTES}


def test_route_walk_every_state_changing_route_requires_csrf(s1a, stateful_routes):
    """Logged-in owner, REAL ids she may write to, no X-CSRF-Token:
    the only thing that may stop the request is the CSRF check."""
    a = s1a.alice
    real = {"wid": a.wid, "workspace_id": a.wid, "company_id": a.wid,
            "job_id": s1a.ids["job"], "bible_id": s1a.ids["bible_draft"],
            "user_id": s1a.vic.user_id, "provider": "publer"}
    failures = []
    for (method, path), route in stateful_routes.items():
        if (method, path) in CSRF_KNOWN_GAPS:
            continue
        vals = dict(real)
        if path.startswith("/auth/oauth/"):
            vals["provider"] = "google"
        r = a.client.request(method, _concrete(path, vals),
                             **_body_kwargs(route))
        if r.status_code != 403 or "csrf" not in r.text.lower():
            failures.append(f"{method} {path} -> {r.status_code} {r.text[:80]}")
    assert not failures, "state-changing routes without CSRF:\n" + "\n".join(failures)
    assert len(stateful_routes) >= 15, len(stateful_routes)


@pytest.mark.xfail(strict=False,
                   reason="A1 F11: keys/rotate CSRF is owned by SECURITY S1b")
def test_known_csrf_gap_keys_rotate(s1a):
    r = s1a.alice.client.post("/api/v1/admin/keys/rotate",
                              json={"old_key": "a", "new_key": "b"})
    assert r.status_code == 403


def test_public_allowlist_has_no_stale_entries(s1a):
    present = {(m, p) for m, p, _r in _app_routes(s1a.app)}
    conditional = {("GET", "/_stripe_init_error"), ("GET", "/_oidc_init_error"),
                   ("GET", "/docs/oauth2-redirect")}
    stale = set(PUBLIC_ROUTES) - present - conditional
    assert not stale, f"PUBLIC_ROUTES entries with no route: {stale}"


def test_public_routes_leak_no_tenant_data(s1a):
    # Generate some traffic first so /metrics has labels to show.
    s1a.alice.client.get(f"/api/v1/jobs/{s1a.ids['job']}")
    s1a.anon.get(f"/api/v1/assets/{s1a.ids['asset']}/download")
    secrets = [s1a.alice.wid, s1a.bob.wid, s1a.ids["job"], s1a.ids["asset"],
               s1a.ids["output"], s1a.alice.email, "alice secret topic",
               "bob secret topic", s1a.alice.user_id]
    for (method, path), why in PUBLIC_ROUTES.items():
        if method != "GET" or "{" in path:
            continue
        r = s1a.anon.get(path)
        if r.status_code == 404:  # conditional route not mounted
            continue
        for s in secrets:
            assert s not in r.text, f"{path} leaks {s!r} ({why})"


# ── tenant route table ───────────────────────────────────────────
# (name, method, url template, json body, own-member status)
_JOB_BODY = {"company_id": "{wid}", "topic": "t", "language": "en-US",
             "duration_seconds": 6, "platforms": ["tiktok"]}

TENANT_ROUTES = [
    ("jobs_list_by_company", "GET", "/api/v1/jobs?company_id={wid}", None, 200),
    ("job_create", "POST", "/api/v1/jobs", _JOB_BODY, 200),
    ("job_get", "GET", "/api/v1/jobs/{job}", None, 200),
    ("job_stages", "GET", "/api/v1/jobs/{job}/stages", None, 200),
    ("job_outputs", "GET", "/api/v1/jobs/{job}/outputs", None, 200),
    ("job_retry", "POST", "/api/v1/jobs/{job}/retry", None, 200),
    ("output_download", "GET",
     "/api/v1/jobs/{job_done}/outputs/{output}/download", None, 200),
    ("publish_create", "POST", "/api/v1/jobs/{job_done}/publish",
     {"action": "draft", "platforms": ["tiktok"]}, 200),
    ("publish_list", "GET", "/api/v1/jobs/{job_done}/publish", None, 200),
    ("asset_download", "GET", "/api/v1/assets/{asset}/download", None, 200),
    ("bible_get", "GET", "/api/v1/bibles/{bible}", None, 200),
    ("bible_approve", "POST", "/api/v1/bibles/{bible_draft}/approve", None, 200),
]


def _fill(obj, ids):
    if isinstance(obj, str):
        return obj.format(**ids)
    if isinstance(obj, dict):
        return {k: _fill(v, ids) for k, v in obj.items()}
    return obj


def _call(client, method, url, body, hdr=None):
    kw = {"headers": hdr or {}}
    if body is not None:
        kw["json"] = body
    return client.request(method, url, **kw)


@pytest.mark.parametrize("name,method,url,body,ok",
                         TENANT_ROUTES, ids=[t[0] for t in TENANT_ROUTES])
def test_tenant_route_anonymous_401(s1a, name, method, url, body, ok):
    r = _call(s1a.anon, method, _fill(url, s1a.ids), _fill(body, s1a.ids))
    assert r.status_code == 401, r.text


@pytest.mark.parametrize("name,method,url,body,ok",
                         TENANT_ROUTES, ids=[t[0] for t in TENANT_ROUTES])
def test_tenant_route_other_workspace_member_404(s1a, name, method, url, body, ok):
    bob = s1a.bob
    r = _call(bob.client, method, _fill(url, s1a.ids), _fill(body, s1a.ids),
              bob.hdr)
    assert r.status_code == 404, r.text
    assert s1a.ids["job"] not in r.text and "alice" not in r.text


@pytest.mark.parametrize("name,method,url,body,ok",
                         TENANT_ROUTES, ids=[t[0] for t in TENANT_ROUTES])
def test_tenant_route_own_member_ok(s1a, name, method, url, body, ok):
    a = s1a.alice
    r = _call(a.client, method, _fill(url, s1a.ids), _fill(body, s1a.ids), a.hdr)
    assert r.status_code == ok, r.text


def test_foreign_and_missing_ids_are_indistinguishable(s1a):
    bob = s1a.bob
    for real, fake in [
        (f"/api/v1/jobs/{s1a.ids['job']}", "/api/v1/jobs/job_doesnotexist"),
        (f"/api/v1/assets/{s1a.ids['asset']}/download",
         "/api/v1/assets/ast_doesnotexist/download"),
        (f"/api/v1/bibles/{s1a.ids['bible']}", "/api/v1/bibles/bib_nope"),
        (f"/api/v1/jobs/{s1a.ids['job_done']}/outputs/{s1a.ids['output']}/download",
         f"/api/v1/jobs/{s1a.ids['job_done']}/outputs/out_nope/download"),
    ]:
        r1, r2 = bob.client.get(real), bob.client.get(fake)
        assert (r1.status_code, r1.json()) == (r2.status_code, r2.json()), real


def test_viewer_reads_but_cannot_write(s1a):
    v = s1a.vic
    assert v.client.get(f"/api/v1/jobs/{s1a.ids['job']}").status_code == 200
    assert v.client.get(
        f"/api/v1/assets/{s1a.ids['asset']}/download").status_code == 200
    writes = [
        ("POST", "/api/v1/jobs", _fill(_JOB_BODY, s1a.ids)),
        ("POST", f"/api/v1/jobs/{s1a.ids['job']}/retry", None),
        ("POST", f"/api/v1/jobs/{s1a.ids['job_done']}/publish",
         {"action": "draft", "platforms": ["tiktok"]}),
        ("POST", f"/api/v1/bibles/{s1a.ids['bible_draft']}/approve", None),
        ("POST", f"/api/v1/workspaces/{s1a.ids['wid']}/jobs",
         _fill(_JOB_BODY, s1a.ids)),
        ("POST", f"/api/v1/workspaces/{s1a.ids['wid']}/bibles",
         {"payload": {"name": "v"}}),
        ("PUT", f"/api/v1/workspaces/{s1a.ids['wid']}/credentials/publer",
         {"value": "x"}),
        ("DELETE", f"/api/v1/workspaces/{s1a.ids['wid']}/credentials/publer",
         None),
    ]
    for method, url, body in writes:
        r = _call(v.client, method, url, body, v.hdr)
        assert r.status_code == 403, f"{method} {url} -> {r.status_code}"
    assert s1a.submitted == []


def test_job_list_without_filter_only_returns_own_workspaces(s1a):
    a = s1a.alice.client.get("/api/v1/jobs").json()
    b = s1a.bob.client.get("/api/v1/jobs").json()
    assert {j["workspace_id"] for j in a} == {s1a.alice.wid}
    assert {j["workspace_id"] for j in b} == {s1a.bob.wid}
    assert len(a) == 2 and len(b) == 1
    assert "bob secret topic" not in str(a)


def test_workspace_routes_require_membership_403(s1a):
    bob = s1a.bob
    wid = s1a.alice.wid
    for method, url, body in [
        ("GET", f"/api/v1/workspaces/{wid}/jobs", None),
        ("GET", f"/api/v1/workspaces/{wid}/assets", None),
        ("POST", f"/api/v1/workspaces/{wid}/jobs", _fill(_JOB_BODY, s1a.ids)),
        ("POST", f"/api/v1/workspaces/{wid}/bibles", {"payload": {}}),
    ]:
        r = _call(bob.client, method, url, body, bob.hdr)
        assert r.status_code == 403, f"{method} {url} -> {r.status_code}"


# ── downloads: HMAC sidecar + headers ───────────────────────────

def test_download_headers_nosniff_and_attachment(s1a):
    r = s1a.alice.client.get(f"/api/v1/assets/{s1a.ids['asset']}/download")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-type"].startswith("image/png")
    r = s1a.alice.client.get(
        f"/api/v1/jobs/{s1a.ids['job_done']}/outputs/{s1a.ids['output']}/download")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("attachment")


def test_download_forged_sidecar_404(s1a):
    from control_plane import artifact_hmac
    asset = s1a.repo.get_asset(s1a.ids["asset"])
    # Sidecar re-tagged for Bob's workspace: the blob no longer
    # verifies as Alice's.
    artifact_hmac.write_sidecar(storage_root=str(s1a.storage),
                                storage_key=asset["storage_key"],
                                workspace_id=s1a.bob.wid)
    r = s1a.alice.client.get(f"/api/v1/assets/{s1a.ids['asset']}/download")
    assert r.status_code == 404
    # Random bytes in the sidecar.
    (s1a.storage / (asset["storage_key"] + ".hmac")).write_bytes(b"\x00" * 32)
    r = s1a.alice.client.get(f"/api/v1/assets/{s1a.ids['asset']}/download")
    assert r.status_code == 404


def test_download_missing_sidecar_404(s1a):
    out = s1a.repo.get_output(s1a.ids["output"])
    (s1a.storage / (out["storage_key"] + ".hmac")).unlink()
    r = s1a.alice.client.get(
        f"/api/v1/jobs/{s1a.ids['job_done']}/outputs/{s1a.ids['output']}/download")
    assert r.status_code == 404


def test_download_db_row_pointing_at_foreign_blob_404(s1a):
    """A tampered asset row in Alice's workspace that points at Bob's
    blob: Bob's sidecar binds the key to Bob, so Alice gets 404."""
    bob_key = s1a.bob_asset["storage_key"]
    with s1a.repo.tx() as c:
        c.execute("UPDATE assets SET storage_key=? WHERE asset_id=?",
                  (bob_key, s1a.ids["asset"]))
    r = s1a.alice.client.get(f"/api/v1/assets/{s1a.ids['asset']}/download")
    assert r.status_code == 404
    assert b"bob private" not in r.content


def test_download_storage_key_escape_404(s1a, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret")
    with s1a.repo.tx() as c:
        c.execute("UPDATE assets SET storage_key=? WHERE asset_id=?",
                  ("../outside.txt", s1a.ids["asset"]))
    r = s1a.alice.client.get(f"/api/v1/assets/{s1a.ids['asset']}/download")
    assert r.status_code == 404
    assert "host secret" not in r.text


def test_download_unsafe_stored_mime_served_as_octet_stream(s1a):
    key = s1a.blob(s1a.alice.wid, "workspaces/alice-ws/assets/ast_h_x.html",
                   b"<script>alert(1)</script>")
    a = s1a.repo.create_asset(workspace_id=s1a.alice.wid, kind="misc",
                              filename="x.html", storage_key=key,
                              mime_type="text/html", size_bytes=25,
                              checksum="0" * 64)
    r = s1a.alice.client.get(f"/api/v1/assets/{a['asset_id']}/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["x-content-type-options"] == "nosniff"


# ── uploads: type allow-list, magic bytes, size cap ─────────────

def _upload(user, *, legacy=False, kind="logo", name="a.png",
            data=b"\x89PNG\r\n\x1a\n" + b"x" * 16, ctype="image/png"):
    url = (f"/api/v1/companies/{user.wid}/assets" if legacy
           else f"/api/v1/workspaces/{user.wid}/assets")
    return user.client.post(f"{url}?kind={kind}", headers=user.hdr,
                            files={"file": (name, io.BytesIO(data), ctype)})


@pytest.mark.parametrize("legacy", [False, True], ids=["workspace", "legacy"])
def test_upload_ok_writes_sidecar_and_downloads(s1a, legacy):
    r = _upload(s1a.alice, legacy=legacy)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["mime_type"] == "image/png"
    assert (s1a.storage / (a["storage_key"] + ".hmac")).is_file()
    d = s1a.alice.client.get(f"/api/v1/assets/{a['asset_id']}/download")
    assert d.status_code == 200
    assert d.content.startswith(b"\x89PNG")
    assert s1a.bob.client.get(
        f"/api/v1/assets/{a['asset_id']}/download").status_code == 404


@pytest.mark.parametrize("legacy", [False, True], ids=["workspace", "legacy"])
@pytest.mark.parametrize("kind,ctype,data", [
    ("logo", "text/plain", b"hello"),
    ("misc", "text/html", b"<html><script>1</script></html>"),
    ("misc", "image/svg+xml", b"<svg onload=alert(1)></svg>"),
    ("misc", "application/javascript", b"alert(1)"),
    ("misc", "application/octet-stream", b"\x7fELF"),
    ("video", "image/png", b"\x89PNG\r\n\x1a\n"),
    ("logo", "", b"\x89PNG\r\n\x1a\n"),
], ids=["text-as-logo", "html", "svg", "js", "octet", "png-as-video", "no-type"])
def test_upload_type_not_allowed_415(s1a, legacy, kind, ctype, data):
    r = _upload(s1a.alice, legacy=legacy, kind=kind, data=data, ctype=ctype,
                name="f.bin")
    assert r.status_code == 415, r.text


@pytest.mark.parametrize("ctype,data", [
    ("image/png", b"<html>not a png</html>"),
    ("image/jpeg", b"\x89PNG\r\n\x1a\n"),
    ("application/pdf", b"<script>alert(1)</script>"),
    ("video/mp4", b"<!doctype html>.........."),
    ("text/plain", b"MZ\x00\x00binary"),
], ids=["html-as-png", "png-as-jpeg", "html-as-pdf", "html-as-mp4", "bin-as-text"])
def test_upload_bytes_must_match_declared_type_415(s1a, ctype, data):
    r = _upload(s1a.alice, kind="misc", data=data, ctype=ctype, name="f")
    assert r.status_code == 415, r.text
    assert not list((s1a.storage / "workspaces").rglob("*_f"))


def test_upload_unknown_kind_400(s1a):
    r = _upload(s1a.alice, kind="exe")
    assert r.status_code == 400


def test_upload_size_cap_413_and_nothing_left_on_disk(s1a, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_MAX_UPLOAD_MB", "1")
    big = b"\x89PNG\r\n\x1a\n" + b"x" * (1024 * 1024 + 10)
    r = _upload(s1a.alice, data=big, name="big.png")
    assert r.status_code == 413, r.text
    assert not list(s1a.storage.rglob("*_big.png"))
    ok = _upload(s1a.alice, data=big[: 512 * 1024], name="half.png")
    assert ok.status_code == 200, ok.text


def test_upload_default_cap_is_200mb(monkeypatch):
    from api import upload_policy
    monkeypatch.delenv("VIDEO_FACTORY_MAX_UPLOAD_MB", raising=False)
    assert upload_policy.max_upload_bytes() == 200 * 1024 * 1024
    monkeypatch.setenv("VIDEO_FACTORY_MAX_UPLOAD_MB", "junk")
    assert upload_policy.max_upload_bytes() == 200 * 1024 * 1024


def test_upload_viewer_403_and_other_workspace_403(s1a):
    v = s1a.vic
    assert _upload(v).status_code == 403
    bob = s1a.bob
    r = bob.client.post(f"/api/v1/workspaces/{s1a.alice.wid}/assets?kind=logo",
                        headers=bob.hdr,
                        files={"file": ("a.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"),
                                        "image/png")})
    assert r.status_code == 403


# ── /health mock_only reflects the live gate (A2 F-17) ──────────

@pytest.mark.parametrize("live,smoke,mock_only", [
    (None, None, True),
    ("true", None, True),
    (None, "true", True),
    ("true", "true", False),
    ("false", "true", True),
])
def test_health_mock_only_reflects_live_gate(s1a, monkeypatch, live, smoke,
                                             mock_only):
    for name, val in (("LIVE_PROVIDER_TESTS", live),
                      ("SMOKE_TEST_PASSED", smoke)):
        if val is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, val)
    r = s1a.anon.get("/health")
    assert r.status_code == 200
    assert r.json()["mock_only"] is mock_only


# ── backfill helper for blobs written before every path tagged them ──

def test_backfill_writes_only_missing_sidecars(s1a):
    from control_plane import artifact_hmac
    # A legacy blob with no sidecar (pre-S1a legacy upload route).
    key = s1a.blob(s1a.alice.wid, "companies/alice-ws/assets/ast_l_old.png",
                   b"\x89PNG\r\n\x1a\nold", sidecar_wid=None)
    legacy = s1a.repo.create_asset(workspace_id=s1a.alice.wid, kind="logo",
                                   filename="old.png", storage_key=key,
                                   mime_type="image/png", size_bytes=11,
                                   checksum="0" * 64)
    # A tampered sidecar must be reported, never "repaired".
    asset = s1a.repo.get_asset(s1a.ids["asset"])
    tampered = s1a.storage / (asset["storage_key"] + ".hmac")
    tampered.write_bytes(b"\x01" * 32)
    url = f"/api/v1/assets/{legacy['asset_id']}/download"
    assert s1a.alice.client.get(url).status_code == 404

    dry = artifact_hmac.backfill_missing_sidecars(
        s1a.repo, storage_root=str(s1a.storage))
    assert dry["missing"] == 1 and dry["written"] == 0
    assert dry["mismatched"] == 1
    assert s1a.alice.client.get(url).status_code == 404

    done = artifact_hmac.backfill_missing_sidecars(
        s1a.repo, storage_root=str(s1a.storage), apply=True)
    assert done["written"] == 1 and done["mismatched"] == 1
    assert s1a.alice.client.get(url).status_code == 200
    assert s1a.bob.client.get(url).status_code == 404
    assert tampered.read_bytes() == b"\x01" * 32
    assert s1a.alice.client.get(
        f"/api/v1/assets/{s1a.ids['asset']}/download").status_code == 404


def test_backfill_skips_keys_outside_storage(s1a, tmp_path):
    from control_plane import artifact_hmac
    (tmp_path / "evil.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    with s1a.repo.tx() as c:
        c.execute("UPDATE assets SET storage_key=? WHERE asset_id=?",
                  ("../evil.png", s1a.ids["asset"]))
    stats = artifact_hmac.backfill_missing_sidecars(
        s1a.repo, storage_root=str(s1a.storage), apply=True)
    assert stats["unsafe_key"] == 1
    assert not (tmp_path / "evil.png.hmac").exists()


def test_backfill_script_refuses_apply_with_dev_key(s1a, monkeypatch, capsys):
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "scripts" / \
        "backfill_artifact_sidecars.py"
    spec = importlib.util.spec_from_file_location("_bf", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([]) == 0
    assert '"apply": false' in capsys.readouterr().out
    monkeypatch.delenv("VIDEO_FACTORY_SECRET_KEY", raising=False)
    monkeypatch.delenv("VIDEO_FACTORY_DB_KEY", raising=False)
    assert mod.main(["--apply"]) == 2
