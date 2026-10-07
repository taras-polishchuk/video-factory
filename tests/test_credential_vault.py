"""Tests for the per-workspace credential vault + UI-facing API.

Wave 2: the vault now binds the per-blob HMAC tag to a
workspace_id.  Tests use the new ``encrypt(wid, provider, value)``
/ ``decrypt(wid, provider, blob)`` signature.
"""
from __future__ import annotations

import hashlib
import os
import pathlib

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from control_plane.secret_vault import (
    SecretKeyMissing,
    WorkspaceMismatch,
    ProviderMismatch,
    encrypt,
    decrypt,
    fingerprint as fp,
    mask_value,
)


# Reuse the env fixture from test_api.py
from tests.test_api import env as env  # noqa: E402,F401


@pytest.fixture
def key_env(monkeypatch):
    """Set a stable VIDEO_FACTORY_SECRET_KEY for the duration of a test."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", key)
    return key


# Test workspace id used by vault-only tests (no repo required).
_TEST_WID = "wks_testvault"


# ── Vault primitives ─────────────────────────────────────────────


def test_encrypt_decrypt_round_trip(key_env):
    blob = encrypt(_TEST_WID, "heygen_direct", "secret_value_xyz")
    assert decrypt(_TEST_WID, "heygen_direct", blob) == "secret_value_xyz"


def test_encrypt_rejects_empty(key_env):
    with pytest.raises(ValueError):
        encrypt(_TEST_WID, "heygen_direct", "")


def test_decrypt_rejects_provider_mismatch(key_env):
    blob = encrypt(_TEST_WID, "heygen_direct", "secret_value_xyz")
    with pytest.raises(ProviderMismatch):
        decrypt(_TEST_WID, "comfy_cloud", blob)


def test_decrypt_rejects_short_blob(key_env):
    with pytest.raises(Exception):
        decrypt(_TEST_WID, "heygen_direct", b"short")


def test_vault_refuses_without_key(monkeypatch):
    monkeypatch.delenv("VIDEO_FACTORY_SECRET_KEY", raising=False)
    with pytest.raises(SecretKeyMissing):
        encrypt(_TEST_WID, "heygen_direct", "x")


def test_fingerprint_is_stable_short_hash():
    """Same plaintext → same fingerprint; different plaintext → different fingerprint."""
    f1 = fp("hello")
    f2 = fp("hello")
    f3 = fp("world")
    assert f1 == f2
    assert f1 != f3
    assert len(f1) == 16


def test_mask_value_keeps_last_four():
    assert mask_value("") == ""
    # ≤4 chars: fully masked (cannot safely reveal ANY of it)
    assert mask_value("abc") == "***"
    assert mask_value("abcd") == "****"
    # >4 chars: keep last 4, mask the rest
    assert mask_value("abcdefgh") == "****efgh"


# ── Repo: encrypted CRUD ─────────────────────────────────────────


def test_repo_credential_round_trip(tmp_path: pathlib.Path, key_env):
    from core.repo import Repo
    repo = Repo(str(tmp_path / "r.db"))
    c = repo.create_company(slug="acme", name="Acme")
    blob = encrypt(c["company_id"], "heygen_direct", "test_secret_12345")
    rec = repo.upsert_credential(
        company_id=c["company_id"], provider="heygen_direct",
        fingerprint_str=fp("test_secret_12345"),
        encrypted_blob=blob, updated_by="alice",
    )
    assert rec["provider"] == "heygen_direct"
    assert rec["has_value"] is True
    assert "value" not in rec
    assert rec["fingerprint"] == fp("test_secret_12345")
    # Decrypt round-trip via repo
    plain = repo.decrypt_credential(
        company_id=c["company_id"], provider="heygen_direct",
    )
    assert plain == "test_secret_12345"


def test_repo_credential_unknown_provider(tmp_path: pathlib.Path, key_env):
    from core.repo import Repo
    repo = Repo(str(tmp_path / "r.db"))
    c = repo.create_company(slug="acme", name="Acme")
    with pytest.raises(ValueError):
        repo.upsert_credential(
            company_id=c["company_id"], provider="not_a_real_provider",
            fingerprint_str="x", encrypted_blob=b"x"*32, updated_by="alice",
        )


def test_repo_credential_upsert_replaces_existing(tmp_path: pathlib.Path, key_env):
    from core.repo import Repo
    repo = Repo(str(tmp_path / "r.db"))
    c = repo.create_company(slug="acme", name="Acme")
    blob1 = encrypt(c["company_id"], "heygen_direct", "first_secret")
    repo.upsert_credential(
        company_id=c["company_id"], provider="heygen_direct",
        fingerprint_str=fp("first_secret"), encrypted_blob=blob1, updated_by="alice",
    )
    blob2 = encrypt(c["company_id"], "heygen_direct", "second_secret")
    rec = repo.upsert_credential(
        company_id=c["company_id"], provider="heygen_direct",
        fingerprint_str=fp("second_secret"), encrypted_blob=blob2, updated_by="alice",
    )
    assert rec["fingerprint"] == fp("second_secret")
    # Only one row
    assert len(repo.list_credentials(c["company_id"])) == 1
    # Decrypted value reflects the latest write
    plain = repo.decrypt_credential(
        company_id=c["company_id"], provider="heygen_direct",
    )
    assert plain == "second_secret"


def test_repo_credential_cross_workspace_isolation(tmp_path: pathlib.Path, key_env):
    """Workspace A's credential is NOT visible to workspace B."""
    from core.repo import Repo, NotFound
    repo = Repo(str(tmp_path / "r.db"))
    a = repo.create_company(slug="a", name="A")
    b = repo.create_company(slug="b", name="B")
    blob = encrypt(a["company_id"], "heygen_direct", "secret_for_a")
    repo.upsert_credential(
        company_id=a["company_id"], provider="heygen_direct",
        fingerprint_str=fp("secret_for_a"), encrypted_blob=blob, updated_by="alice",
    )
    # Workspace B has no credential for heygen_direct
    assert repo.list_credentials(b["company_id"]) == []
    with pytest.raises(NotFound):
        repo.decrypt_credential(
            company_id=b["company_id"], provider="heygen_direct",
        )


def test_repo_credential_delete(tmp_path: pathlib.Path, key_env):
    from core.repo import Repo, NotFound
    repo = Repo(str(tmp_path / "r.db"))
    c = repo.create_company(slug="acme", name="Acme")
    blob = encrypt(c["company_id"], "heygen_direct", "secret_x")
    repo.upsert_credential(
        company_id=c["company_id"], provider="heygen_direct",
        fingerprint_str=fp("secret_x"), encrypted_blob=blob, updated_by="alice",
    )
    repo.delete_credential(company_id=c["company_id"], provider="heygen_direct")
    with pytest.raises(NotFound):
        repo.get_credential(c["company_id"], "heygen_direct")


# ── API surface ──────────────────────────────────────────────────


def test_api_put_credential_encrypts_server_side(env, key_env):
    """Plaintext flows: client → API → encrypt → repo. Plaintext never
    appears in the response or the repo row.
    """
    client, repo, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    r = client.put(
        f"/api/v1/companies/{cid}/credentials/heygen_direct",
        json={"value": "supersecret"},
        headers={"X-Operator": "alice"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "heygen_direct"
    assert body["has_value"] is True
    assert "supersecret" not in json.dumps(body), "plaintext leaked in response"
    assert body["fingerprint"] == fp("supersecret")
    # Decrypt via repo and confirm
    plain = repo.decrypt_credential(company_id=cid, provider="heygen_direct")
    assert plain == "supersecret"


def test_api_list_credentials_returns_masked_only(env, key_env):
    client, repo, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    client.put(f"/api/v1/companies/{cid}/credentials/heygen_direct",
                json={"value": "supersecret"})
    r = client.get(f"/api/v1/companies/{cid}/credentials")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert "supersecret" not in json.dumps(items), "plaintext leaked in list"


def test_api_put_credential_unknown_provider(env, key_env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    r = client.put(
        f"/api/v1/companies/{cid}/credentials/not_a_real_provider",
        json={"value": "x"},
    )
    assert r.status_code == 400


def test_api_put_credential_empty_value(env, key_env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    r = client.put(
        f"/api/v1/companies/{cid}/credentials/heygen_direct",
        json={"value": ""},
    )
    assert r.status_code == 400


def test_api_put_credential_writes_audit_row(env, key_env):
    """The credentials.upserted audit row records the actor.

    Wave 2 follow-up (t_d76b7c0c, MAJOR #5): the legacy
    alias PUT now requires auth + CSRF + membership. The
    actor recorded in the audit row is the authenticated
    user's email (the X-Operator header was a v0 placeholder).
    """
    client, repo, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    client.put(
        f"/api/v1/companies/{cid}/credentials/heygen_direct",
        json={"value": "audit_me"},
    )
    # Audit table should contain a credentials.upserted row
    rows = repo._conn().execute(
        "SELECT job_id, event, context_json FROM runner_audit WHERE event=?",
        ("credentials.upserted",),
    ).fetchall()
    assert len(rows) == 1
    # Actor is the authenticated user's email (set by
    # _AuthedClient's auto-signup as tester@video-factory.test).
    assert "tester@video-factory.test" in rows[0][2]


def test_api_delete_credential_writes_audit_row(env, key_env):
    client, repo, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    client.put(f"/api/v1/companies/{cid}/credentials/heygen_direct",
                json={"value": "x"}, headers={"X-Operator": "carol"})
    r = client.delete(f"/api/v1/companies/{cid}/credentials/heygen_direct",
                       headers={"X-Operator": "carol"})
    assert r.status_code == 200
    rows = repo._conn().execute(
        "SELECT event, context_json FROM runner_audit WHERE event=?",
        ("credentials.deleted",),
    ).fetchall()
    assert len(rows) == 1


def test_api_delete_credential_404(env, key_env):
    client, _, _ = env
    cid = client.post("/api/v1/companies", json={"slug": "a", "name": "A"}).json()["company_id"]
    r = client.delete(f"/api/v1/companies/{cid}/credentials/heygen_direct")
    assert r.status_code == 404


# ── Render plane integration ──────────────────────────────────────


def test_heygen_adapter_uses_vault_secret_provider(key_env, monkeypatch):
    """When constructed with a SecretProvider, the adapter reads the
    API key from the vault at submit() time, not from env.
    """
    monkeypatch.setenv("LIVE_PROVIDER_TESTS", "true")
    monkeypatch.setenv("SMOKE_TEST_PASSED", "true")
    monkeypatch.delenv("HEYGEN_API_KEY", raising=False)

    from render_plane.secret_provider import EnvOnlySecretProvider
    captured = {}

    class _CapturingProvider(EnvOnlySecretProvider):
        def resolve(self, *, company_id, provider, env_var):
            v = super().resolve(company_id=company_id, provider=provider, env_var=env_var)
            captured["company_id"] = company_id
            captured["provider"] = provider
            captured["env_var"] = env_var
            return v or "vault_supplied_key"

    from render_plane.heygen_direct import HeyGenDirectAdapter
    a = HeyGenDirectAdapter(
        company_id="cmp_test", secret_provider=_CapturingProvider(),
    )
    # We can't actually submit (HTTP path), but we can verify that
    # _resolve_api_key returns the vault-supplied value.
    assert a._resolve_api_key() == "vault_supplied_key"
    assert captured["company_id"] == "cmp_test"
    assert captured["provider"] == "heygen_direct"


def test_heygen_adapter_falls_back_to_env_when_vault_empty(key_env, monkeypatch):
    """Vault returns None → adapter falls back to env var."""
    monkeypatch.setenv("HEYGEN_API_KEY", "env_supplied_key")
    from render_plane.heygen_direct import HeyGenDirectAdapter
    a = HeyGenDirectAdapter(company_id="cmp_test")
    assert a._resolve_api_key() == "env_supplied_key"


def test_heygen_adapter_returns_none_when_both_empty(key_env, monkeypatch):
    """Vault and env both empty → adapter surfaces ProviderError on submit."""
    monkeypatch.delenv("HEYGEN_API_KEY", raising=False)
    from render_plane.heygen_direct import HeyGenDirectAdapter
    a = HeyGenDirectAdapter(company_id="cmp_test")
    assert a._resolve_api_key() is None


# ── Cross-cutting ────────────────────────────────────────────────


import json  # for the inline leak checks above