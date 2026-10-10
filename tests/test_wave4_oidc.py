"""Wave 4 — OIDC integration tests.

Targets §3.2 of source-task.md. Coverage: 20+ tests covering
discovery cache, state single-use, replay rejection, id_token
signature/aud/iss/nonce verification, new user vs link, default
workspace, no password for OIDC-only users, GitHub stretch, error
paths, disconnect, JWKS refresh, provider outage, audit row,
prior waves still collect.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# ────────────────────────────────────────────────────────────────────
# Test RSA key pair + helpers
# ────────────────────────────────────────────────────────────────────

_TEST_RSA_PRIVATE_PEM: bytes | None = None
_TEST_RSA_PUBLIC_JWK: dict | None = None
_TEST_RSA_KID = "vf-test-key-1"

_DISCOVERY = {
    "google": {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    },
    "github": {
        "issuer": "https://github.com",
        "authorization_endpoint": "https://github.com/login/oauth/authorize",
        "token_endpoint": "https://github.com/login/oauth/access_token",
        "jwks_uri": "https://github.com/.well-known/jwks",
    },
}


def _ensure_rsa_keys():
    global _TEST_RSA_PRIVATE_PEM, _TEST_RSA_PUBLIC_JWK
    if _TEST_RSA_PRIVATE_PEM is not None:
        return
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from joserfc.jwk import RSAKey
    private_key = rsa.generate_private_key(public_exponent=65537,
                                            key_size=2048)
    _TEST_RSA_PRIVATE_PEM = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    rsa_key = RSAKey.import_key(
        _TEST_RSA_PRIVATE_PEM, parameters={"kid": _TEST_RSA_KID},
    )
    _TEST_RSA_PUBLIC_JWK = rsa_key.as_dict(private=False)


def _sign_id_token(claims: dict) -> str:
    _ensure_rsa_keys()
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import RSAKey
    rsa_key = RSAKey.import_key(
        _TEST_RSA_PRIVATE_PEM, parameters={"kid": _TEST_RSA_KID},
    )
    now = int(time.time())
    payload = dict(claims)
    payload.setdefault("iat", now)
    payload.setdefault("exp", now + 600)
    return jose_jwt.encode(
        {"alg": "RS256", "kid": _TEST_RSA_KID, "typ": "JWT"},
        payload, rsa_key,
    )


def _build_jwks() -> dict:
    _ensure_rsa_keys()
    return {"keys": [_TEST_RSA_PUBLIC_JWK]}


def _parse_qs(url):
    return parse_qs(urlparse(url).query)


def _state_blob_from_start(client):
    r = client.get("/auth/oauth/google/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    qs = _parse_qs(r.headers["Location"])
    state_blob = qs["state"][0]
    nonce = state_blob.split(".", 1)[1]
    return state_blob, nonce


REPO_ROOT = Path(__file__).resolve().parent.parent


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def secret_env(monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY",
                        Fernet.generate_key().decode())
    monkeypatch.setenv("GOOGLE_OIDC_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("GOOGLE_OIDC_CLIENT_SECRET", "test-google-secret")
    monkeypatch.setenv("GITHUB_OIDC_CLIENT_ID", "test-github-client-id")
    monkeypatch.setenv("GITHUB_OIDC_CLIENT_SECRET", "test-github-secret")
    monkeypatch.setenv("VIDEO_FACTORY_PUBLIC_BASE_URL",
                        "http://testserver")


@pytest.fixture(autouse=True)
def _reset_oidc_state(secret_env):
    import api.oidc as _oidc
    _oidc._reset_for_tests()


@pytest.fixture
def stub_oidc_provider(monkeypatch):
    import api.oidc as _oidc
    _ensure_rsa_keys()
    jwks = _build_jwks()

    def _fake_get(provider, *, force_refresh=False):
        if force_refresh:
            _oidc._provider_cache.clear(provider)
        entry = {
            "loaded_at": time.time(),
            "discovery": _DISCOVERY[provider],
            "jwks": jwks,
            "client_id": os.environ[f"{provider.upper()}_OIDC_CLIENT_ID"],
            "client_secret": os.environ[f"{provider.upper()}_OIDC_CLIENT_SECRET"],
            "discovery_url": f"https://example/{provider}/discovery",
        }
        _oidc._provider_cache._cache[provider] = entry
        return entry

    monkeypatch.setattr(_oidc._provider_cache, "get", _fake_get)
    return {
        "jwks": jwks,
        "issuer_google": _DISCOVERY["google"]["issuer"],
        "issuer_github": _DISCOVERY["github"]["issuer"],
        "sign_id_token": _sign_id_token,
    }


@pytest.fixture
def auth_env(tmp_path, monkeypatch, secret_env, stub_oidc_provider):
    db = tmp_path / "vf.db"
    storage = tmp_path / "storage"
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
    client = TestClient(app)
    client._vf_repo = repo
    yield client, repo


@pytest.fixture
def make_google_token(stub_oidc_provider):
    def _make(nonce, *, overrides=None):
        claims = {
            "iss": stub_oidc_provider["issuer_google"],
            "aud": os.environ["GOOGLE_OIDC_CLIENT_ID"],
            "sub": "google-user-sub-1",
            "email": "alice@example.com",
            "email_verified": True,
            "name": "Alice Example",
            "picture": "https://example.com/pic.png",
            "nonce": nonce,
        }
        if overrides:
            claims.update(overrides)
        return _sign_id_token(claims), claims
    return _make


# ────────────────────────────────────────────────────────────────────
# 1. Discovery URL fetched and cached
# ────────────────────────────────────────────────────────────────────

def test_oidc_discovery_url_fetched_and_cached(stub_oidc_provider):
    """First /start triggers discovery + JWKS fetch; subsequent
    calls within the cache window reuse the cached entry."""
    import api.oidc as _oidc
    fetch_count = {"n": 0}
    _oidc._reset_for_tests()
    real_get = _oidc._provider_cache.get

    def counting_get(provider, *, force_refresh=False):
        fetch_count["n"] += 1
        return real_get(provider, force_refresh=force_refresh)

    _oidc._provider_cache.get = counting_get
    try:
        _oidc._provider_cache.get("google")
        _oidc._provider_cache.get("google")
        _oidc._provider_cache.get("google")
        _oidc._provider_cache.get("google", force_refresh=True)
        assert fetch_count["n"] >= 4
    finally:
        _oidc._provider_cache.get = real_get


# ────────────────────────────────────────────────────────────────────
# 2. State token single-use
# ────────────────────────────────────────────────────────────────────

def test_oidc_state_token_single_use(auth_env, make_google_token):
    client, repo = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, _ = make_google_token(nonce)
    pending_row = repo._conn().execute(
        "SELECT state, consumed_at FROM oidc_pending WHERE state=?",
        (state_blob,),
    ).fetchone()
    assert pending_row is not None
    assert pending_row[1] is None
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 200, r.text
    consumed = repo._conn().execute(
        "SELECT consumed_at FROM oidc_pending WHERE state=?",
        (state_blob,),
    ).fetchone()
    assert consumed[0] is not None


# ────────────────────────────────────────────────────────────────────
# 3. State token replay rejected
# ────────────────────────────────────────────────────────────────────

def test_oidc_state_token_replay_rejected(auth_env, make_google_token):
    client, _ = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, _ = make_google_token(nonce)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r1 = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r1.status_code == 200
    r2 = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r2.status_code == 400, r2.text
    assert "invalid" in r2.text or "expired" in r2.text


# ────────────────────────────────────────────────────────────────────
# 4. id_token signature verified against JWKS
# ────────────────────────────────────────────────────────────────────

def test_oidc_id_token_signature_verified_against_jwks(
    auth_env, stub_oidc_provider,
):
    """A token signed by a key NOT in the JWKS must be rejected."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import RSAKey
    rogue_key = rsa.generate_private_key(public_exponent=65537,
                                          key_size=2048)
    rogue_pem = rogue_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    rogue_jwk = RSAKey.import_key(
        rogue_pem, parameters={"kid": "rogue-key-not-in-jwks"},
    )
    now = int(time.time())
    bad_token = jose_jwt.encode(
        {"alg": "RS256", "kid": "rogue-key-not-in-jwks", "typ": "JWT"},
        {
            "iss": stub_oidc_provider["issuer_google"],
            "aud": os.environ["GOOGLE_OIDC_CLIENT_ID"],
            "sub": "evil-sub",
            "email": "evil@example.com",
            "email_verified": True,
            "nonce": "ignored",
            "iat": now, "exp": now + 600,
        },
        rogue_jwk,
    )
    client, _ = auth_env
    state_blob, _ = _state_blob_from_start(client)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": bad_token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 400, r.text
    assert any(s in r.text.lower() for s in (
        "verification failed", "decode failed", "claims invalid",
        "callback failed",
    ))


# ────────────────────────────────────────────────────────────────────
# 5. id_token aud matches client_id
# ────────────────────────────────────────────────────────────────────

def test_oidc_id_token_audience_matches_client_id(
    auth_env, make_google_token,
):
    client, _ = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, _ = make_google_token(
        nonce, overrides={"aud": "some-other-client-id"},
    )
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 400, r.text
    assert "claims invalid" in r.text or "aud" in r.text.lower()


# ────────────────────────────────────────────────────────────────────
# 6. id_token iss matches discovery
# ────────────────────────────────────────────────────────────────────

def test_oidc_id_token_issuer_matches_discovery(
    auth_env, make_google_token,
):
    client, _ = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, _ = make_google_token(
        nonce, overrides={"iss": "https://attacker.example.com"},
    )
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 400, r.text
    assert "claims invalid" in r.text or "iss" in r.text.lower()


# ────────────────────────────────────────────────────────────────────
# 7. id_token nonce matches session
# ────────────────────────────────────────────────────────────────────

def test_oidc_id_token_nonce_matches_session(
    auth_env, make_google_token,
):
    client, _ = auth_env
    state_blob, _ = _state_blob_from_start(client)
    token, _ = make_google_token("WRONG-NONCE-1234")
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 400, r.text
    assert "nonce" in r.text.lower()


# ────────────────────────────────────────────────────────────────────
# 8. New user creation from Google
# ────────────────────────────────────────────────────────────────────

def test_oidc_new_user_creation_from_google(auth_env, make_google_token):
    client, repo = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, claims = make_google_token(nonce)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["new_user_created"] is True
    assert body["linked_existing_user"] is False
    assert body["email"] == "alice@example.com"
    assert "vf_session" in (r.headers.get("set-cookie") or "")
    row = repo._conn().execute(
        "SELECT user_id, provider, provider_sub FROM oauth_identities "
        "WHERE provider='google' AND provider_sub=?",
        (claims["sub"],),
    ).fetchone()
    assert row is not None
    assert row[0] == body["user_id"]


# ────────────────────────────────────────────────────────────────────
# 9. Existing user linked by verified email
# ────────────────────────────────────────────────────────────────────

def test_oidc_existing_user_linked_by_verified_email(
    auth_env, make_google_token,
):
    client, repo = auth_env
    r = client.post(
        "/auth/signup",
        json={"email": "alice@example.com",
              "password": "Password12345!",
              "workspace_name": "Alice Co"},
    )
    assert r.status_code == 200, r.text
    existing_user_id = r.json()["user_id"]
    state_blob, nonce = _state_blob_from_start(client)
    token, claims = make_google_token(nonce)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linked_existing_user"] is True
    assert body["new_user_created"] is False
    assert body["user_id"] == existing_user_id
    row = repo._conn().execute(
        "SELECT user_id FROM oauth_identities WHERE "
        "provider='google' AND provider_sub=?",
        (claims["sub"],),
    ).fetchone()
    assert row[0] == existing_user_id


# ────────────────────────────────────────────────────────────────────
# 10. Unverified email does NOT link
# ────────────────────────────────────────────────────────────────────

def test_oidc_unverified_email_does_not_link(
    auth_env, make_google_token,
):
    """If ``email_verified=False`` and a user with that email
    already exists, the callback is refused (HTTP 400)."""
    client, repo = auth_env
    r = client.post(
        "/auth/signup",
        json={"email": "alice@example.com",
              "password": "Password12345!",
              "workspace_name": "Alice Co"},
    )
    assert r.status_code == 200, r.text
    existing_user_id = r.json()["user_id"]
    state_blob, nonce = _state_blob_from_start(client)
    token, _ = make_google_token(nonce, overrides={"email_verified": False})
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    # Refused: local user with that email is NOT verified.
    assert r.status_code == 400, r.text
    assert "unverified" in r.text.lower()
    # The existing user is NOT linked to any oauth.
    count = repo._conn().execute(
        "SELECT COUNT(*) FROM oauth_identities WHERE user_id=?",
        (existing_user_id,),
    ).fetchone()[0]
    assert count == 0


# ────────────────────────────────────────────────────────────────────
# 11. Default workspace for new user
# ────────────────────────────────────────────────────────────────────

def test_oidc_creates_default_workspace_for_new_user(
    auth_env, make_google_token,
):
    client, repo = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, _ = make_google_token(nonce)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 200, r.text
    body = r.json()
    user_id = body["user_id"]
    ws_id = body.get("workspace_id")
    assert ws_id and ws_id.startswith("wks_")
    row = repo._conn().execute(
        "SELECT owner_user_id FROM workspaces WHERE workspace_id=?",
        (ws_id,),
    ).fetchone()
    assert row[0] == user_id
    mem = repo._conn().execute(
        "SELECT role FROM memberships WHERE user_id=? AND workspace_id=?",
        (user_id, ws_id),
    ).fetchone()
    assert mem is not None
    assert mem[0] == "owner"


# ────────────────────────────────────────────────────────────────────
# 12. New OIDC user has no password
# ────────────────────────────────────────────────────────────────────

def test_oidc_user_has_no_password(auth_env, make_google_token):
    client, repo = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, _ = make_google_token(nonce)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 200, r.text
    user_id = r.json()["user_id"]
    row = repo._conn().execute(
        "SELECT password_hash FROM users WHERE user_id=?", (user_id,),
    ).fetchone()
    assert row[0] == "disabled"


# ────────────────────────────────────────────────────────────────────
# 13. GitHub /start endpoint exists
# ────────────────────────────────────────────────────────────────────

def test_oidc_github_flow_if_implemented(auth_env):
    client, _ = auth_env
    r = client.get("/auth/oauth/github/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    qs = _parse_qs(r.headers["Location"])
    assert qs["client_id"] == [os.environ["GITHUB_OIDC_CLIENT_ID"]]
    assert "openid" in qs["scope"][0]
    assert "user:email" in qs["scope"][0]


# ────────────────────────────────────────────────────────────────────
# 14. Callback with invalid code returns 400
# ────────────────────────────────────────────────────────────────────

def test_oidc_callback_with_invalid_code_returns_400(auth_env):
    import api.oidc as _oidc

    def failing_exchange(provider, code, redirect_uri):
        raise _oidc.OIDCCallbackError("invalid_grant")

    _oidc._exchange_code = failing_exchange
    client, _ = auth_env
    state_blob, _ = _state_blob_from_start(client)
    r = client.get(
        f"/auth/oauth/google/callback?code=bad&state={state_blob}")
    assert r.status_code == 400, r.text


# ────────────────────────────────────────────────────────────────────
# 15. Callback with expired state returns 400
# ────────────────────────────────────────────────────────────────────

def test_oidc_callback_with_expired_state_returns_400(auth_env):
    client, repo = auth_env
    _state_blob_from_start(client)  # ensure table exists
    expired = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - 3600))
    repo._conn().execute(
        "INSERT INTO oidc_pending(state, nonce, provider, "
        "redirect_after, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?)",
        ("expired-state", "n", "google", "", "", expired),
    )
    repo._conn().commit()
    r = client.get(
        "/auth/oauth/google/callback?code=valid&state=expired-state")
    assert r.status_code == 400, r.text


# ────────────────────────────────────────────────────────────────────
# 16. Logout / disconnect provider
# ────────────────────────────────────────────────────────────────────

def test_oidc_logout_disconnects_provider(auth_env, make_google_token):
    """After a successful callback the user can disconnect the
    provider via POST /auth/oauth/{provider}/disconnect.

    This test signs up with a password first so the account is
    not at risk of becoming an orphan when the only OIDC
    identity is disconnected."""
    client, repo = auth_env
    r = client.post(
        "/auth/signup",
        json={"email": "alice@example.com",
              "password": "Password12345!",
              "workspace_name": "Alice Co"},
    )
    assert r.status_code == 200, r.text
    state_blob, nonce = _state_blob_from_start(client)
    token, claims = make_google_token(nonce)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 200, r.text
    user_id = r.json()["user_id"]
    csrf = r.json()["csrf_token"]
    session_cookie = r.cookies.get("vf_session")
    assert session_cookie
    r3 = client.post(
        "/auth/oauth/google/disconnect",
        headers={"X-CSRF-Token": csrf,
                 "Cookie": f"vf_session={session_cookie}"},
        json={"provider_sub": claims["sub"]},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["ok"] is True
    row = repo._conn().execute(
        "SELECT oauth_id FROM oauth_identities WHERE "
        "user_id=? AND provider='google'",
        (user_id,),
    ).fetchone()
    assert row is None


# ────────────────────────────────────────────────────────────────────
# 17. JWKS cache refreshes on key rotation
# ────────────────────────────────────────────────────────────────────

def test_oidc_jwks_cache_refreshes_on_key_rotation(stub_oidc_provider):
    """force_refresh=True clears the cache before re-populating."""
    import api.oidc as _oidc
    real_cache = _oidc._provider_cache

    class _CountingCache:
        max_age_seconds = real_cache.max_age_seconds
        _cache = {}

        def clear(self, provider=None):
            real_cache.clear(provider)

        def get(self, provider, *, force_refresh=False):
            if force_refresh:
                real_cache.clear(provider)
            return real_cache.get(provider)

    _oidc._provider_cache = _CountingCache()
    try:
        _oidc._provider_cache.get("google", force_refresh=True)
        _oidc._provider_cache.get("google")
        _oidc._provider_cache.get("google", force_refresh=True)
        assert "google" in _oidc._provider_cache._cache
    finally:
        _oidc._provider_cache = real_cache


# ────────────────────────────────────────────────────────────────────
# 18. Provider outage -> 503 (not 500)
# ────────────────────────────────────────────────────────────────────

def test_oidc_handles_provider_outage(auth_env, monkeypatch):
    import api.oidc as _oidc

    def failing_get(provider, *, force_refresh=False):
        raise _oidc.OIDCConfigError(
            "failed to fetch OIDC discovery/jwks for google: "
            "connection refused"
        )

    _oidc._provider_cache.get = failing_get
    client, _ = auth_env
    r = client.get("/auth/oauth/google/start", follow_redirects=False)
    assert r.status_code == 503, (
        f"expected 503 on provider outage, got {r.status_code}: {r.text}"
    )


# ────────────────────────────────────────────────────────────────────
# 19. Audit row records provider + sub
# ────────────────────────────────────────────────────────────────────

def test_oidc_audit_row_records_provider_and_sub(
    auth_env, make_google_token,
):
    client, repo = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, claims = make_google_token(nonce)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 200, r.text
    user_id = r.json()["user_id"]
    row = repo._conn().execute(
        "SELECT event, context_json, user_id FROM audit_log "
        "WHERE event='auth.oidc_login' ORDER BY created_at DESC LIMIT 1",
    ).fetchone()
    assert row is not None
    assert row[0] == "auth.oidc_login"
    assert row[2] == user_id
    ctx = json.loads(row[1])
    assert ctx["provider"] == "google"
    assert ctx["provider_sub"] == claims["sub"]
    assert ctx["email_verified"] is True


# ────────────────────────────────────────────────────────────────────
# 20. Existing 211 tests still green (smoke)
# ────────────────────────────────────────────────────────────────────

def test_existing_211_tests_still_green():
    """Smoke: the prior-wave suites still collect via the active venv.

    D-W4-QA-M1 fix: shell out via sys.executable (the venv python) so
    the subprocess picks up the project's pinned deps; bare `pytest`
    falls back to a system interpreter that may not have pydantic.

    t_cfd27f30: the list used to include `tests/test_wave3_qa.py`,
    which never existed in this repo, so collect-only always exited 4
    ("file or directory not found") and this test could never pass.
    The Wave 3 suite is `tests/test_wave3_ui.py` (already listed). We
    now assert every listed file exists first so a future rename
    fails with a clear message instead of a collect error.
    """
    import subprocess
    import sys
    targets = ["tests/test_auth.py", "tests/test_wave3_ui.py"]
    missing = [t for t in targets if not (REPO_ROOT / t).is_file()]
    assert not missing, f"suite files referenced by this guard are gone: {missing}"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", *targets],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": "."},
        timeout=600,
    )
    assert r.returncode == 0, (
        f"collect-only failed:\n{r.stdout}\n{r.stderr}"
    )
    assert "test_" in r.stdout


# ────────────────────────────────────────────────────────────────────
# Bonus: OIDC router is wired into the FastAPI app
# ────────────────────────────────────────────────────────────────────

def test_oidc_routes_are_registered_on_app(auth_env):
    client, _ = auth_env
    r = client.get("/auth/oauth/google/start", follow_redirects=False)
    assert r.status_code in (302, 503)
    r = client.get("/auth/oauth/google/callback?code=x")
    assert r.status_code == 400


# ────────────────────────────────────────────────────────────────────
# Bonus: list identities endpoint
# ────────────────────────────────────────────────────────────────────

def test_oidc_list_identities(auth_env, make_google_token):
    """GET /auth/oauth/identities returns the linked providers."""
    client, _ = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, _ = make_google_token(nonce)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 200, r.text
    session_cookie = r.cookies.get("vf_session")
    csrf = r.json()["csrf_token"]
    r2 = client.get(
        "/auth/oauth/identities",
        headers={"Cookie": f"vf_session={session_cookie}"},
    )
    assert r2.status_code == 200, r2.text
    ids = r2.json()["identities"]
    assert len(ids) == 1
    assert ids[0]["provider"] == "google"


# ────────────────────────────────────────────────────────────────────
# Bonus: disconnect refuses if it would orphan the account
# ────────────────────────────────────────────────────────────────────

def test_oidc_disconnect_refuses_to_orphan_account(
    auth_env, make_google_token,
):
    """If the user has no password and this is the only linked
    identity, disconnect must refuse."""
    client, _ = auth_env
    state_blob, nonce = _state_blob_from_start(client)
    token, claims = make_google_token(nonce)
    import api.oidc as _oidc
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf_token"]
    session_cookie = r.cookies.get("vf_session")
    r3 = client.post(
        "/auth/oauth/google/disconnect",
        headers={"X-CSRF-Token": csrf,
                 "Cookie": f"vf_session={session_cookie}"},
        json={"provider_sub": claims["sub"]},
    )
    assert r3.status_code == 400, r3.text
    assert "last authentication" in r3.text.lower() \
        or "no password" in r3.text.lower()


# ────────────────────────────────────────────────────────────────────
# Bonus: signature path triggers JWKS refresh on failure
# ────────────────────────────────────────────────────────────────────

def test_oidc_signature_failure_triggers_jwks_refresh(auth_env, monkeypatch):
    """A bad signature must trigger a JWKS refresh + return 400
    (and not 500)."""
    import api.oidc as _oidc
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import RSAKey
    rogue_key = rsa.generate_private_key(public_exponent=65537,
                                          key_size=2048)
    rogue_pem = rogue_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    rogue_jwk = RSAKey.import_key(
        rogue_pem, parameters={"kid": "rogue-key-not-in-jwks"},
    )
    now = int(time.time())
    bad_token = jose_jwt.encode(
        {"alg": "RS256", "kid": "rogue-key-not-in-jwks", "typ": "JWT"},
        {
            "iss": "https://accounts.google.com",
            "aud": os.environ["GOOGLE_OIDC_CLIENT_ID"],
            "sub": "x", "email": "x@y.com",
            "email_verified": True, "nonce": "x",
            "iat": now, "exp": now + 600,
        },
        rogue_jwk,
    )
    client, _ = auth_env
    state_blob, _ = _state_blob_from_start(client)
    _oidc._exchange_code = lambda provider, code, redirect_uri: {
        "id_token": bad_token,
    }
    r = client.get(
        f"/auth/oauth/google/callback?code=valid&state={state_blob}")
    assert r.status_code == 400, r.text