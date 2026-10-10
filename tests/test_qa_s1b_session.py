"""QA S1b adversarial tests (t_8a585bad), written independently of the dev's
tests/test_security_s1b.py.

Attack variants and neighbouring-feature regressions for the S1b fixes
(t_ca20feb0): session rotation, require_csrf, Origin/Referer allow-list,
rate limits + client IP, key-rotation endpoint hygiene, HeyGen webhook
replay, CORS, security headers, PKCE, session-secret rotation, Secure
cookies.

All data is synthetic. No network. Every test builds its own app + DB.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

SECRET = "qa-s1b-session-secret-" + "k" * 40
OLD_SECRET = "qa-s1b-OLD-session-secret-" + "o" * 36
PASSWORD = "Password12345!"
NEW_PASSWORD = "Brand-New-Passw0rd!x"

_ENV_TO_CLEAR = (
    "VIDEO_FACTORY_ENV", "VF_HTTPS", "VIDEO_FACTORY_REQUIRE_ORIGIN",
    "VIDEO_FACTORY_PUBLIC_BASE_URL", "VIDEO_FACTORY_CORS_ORIGINS",
    "VIDEO_FACTORY_TRUSTED_PROXIES", "VIDEO_FACTORY_SESSION_SECRET_PREVIOUS",
    "VIDEO_FACTORY_DEV_EXPOSE_TOKENS", "HEYGEN_WEBHOOK_SECRET",
    "VF_SIGNUP_IP_LIMIT", "VF_SIGNUP_IP_WINDOW_SEC",
    "VF_FORGOT_IP_LIMIT", "VF_FORGOT_IP_WINDOW_SEC",
    "VF_FORGOT_EMAIL_LIMIT", "VF_FORGOT_EMAIL_WINDOW_SEC",
    "VF_RESET_CONSUME_IP_LIMIT", "VF_RESET_CONSUME_IP_WINDOW_SEC",
    "VF_VERIFY_IP_LIMIT", "VF_VERIFY_IP_WINDOW_SEC",
    "VF_KEYS_REAUTH_LIMIT", "VF_KEYS_REAUTH_WINDOW_SEC",
    "VIDEO_FACTORY_DB_KEY", "SENTRY_DSN",
    "GOOGLE_OIDC_CLIENT_ID", "GOOGLE_OIDC_CLIENT_SECRET",
    "GITHUB_OIDC_CLIENT_ID", "GITHUB_OIDC_CLIENT_SECRET",
)


@pytest.fixture()
def make_app(tmp_path, monkeypatch):
    for k in _ENV_TO_CLEAR:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", SECRET)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(tmp_path / "storage"))

    def _make(*, db_key=None, **app_kw):
        from api.main import create_app
        from core.repo import Repo
        from core.runners import JobRunner, Singleton
        repo = (Repo(str(tmp_path / "vf.db"), db_key=db_key) if db_key
                else Repo(str(tmp_path / "vf.db")))
        runner = JobRunner(
            repo=repo, storage_root=str(tmp_path / "storage"), schema={},
            budget_per_video_usd=5.0, budget_per_batch_usd=20.0,
        )
        Singleton.set(runner)
        app = create_app(repo=repo, runner=runner,
                         storage_root=str(tmp_path / "storage"), schema={}, **app_kw)
        return app, repo
    return _make


# ── helpers ──────────────────────────────────────────────────────────

def _hmac(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _csrf(sid: str, secret: str = SECRET) -> str:
    return _hmac(secret, sid)


def _internal(secret: str = SECRET) -> str:
    return _hmac(secret, "vf-internal-caller:v1")


def _ck(sid: str) -> dict:
    return {"Cookie": f"vf_session={sid}"}


def _signup(c, email, *, headers=None, pw=PASSWORD):
    return c.post("/auth/signup", json={"email": email, "password": pw,
                                         "workspace_name": "QA Co"},
                  headers=headers or {})


def _login(c, email, pw=PASSWORD, *, headers=None):
    return c.post("/auth/login", json={"email": email, "password": pw},
                  headers=headers or {})


def _sid(r) -> str:
    return r.cookies.get("vf_session")


def _me(app, sid) -> int:
    return TestClient(app).get("/auth/me", headers=_ck(sid)).status_code


def _count_rows(repo, table, where="1=1", args=()):
    try:
        return repo._conn().execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
    except Exception:  # table not created yet
        return 0


PRE_SESSION_ROUTES = {
    "signup": ("/auth/signup", {"email": "victim-qa@example.com", "password": PASSWORD,
                                 "workspace_name": "V"}),
    "login": ("/auth/login", {"email": "victim-qa@example.com", "password": PASSWORD}),
    "forgot": ("/auth/forgot", {"email": "victim-qa@example.com"}),
    "forgot_consume": ("/auth/forgot/consume", {"token": "x" * 24, "new_password": NEW_PASSWORD}),
    "verify": ("/auth/verify", {"token": "x" * 24}),
}

HOSTILE_ORIGINS = [
    "null",
    "",
    "http://evil.example",
    "http://testserver.evil.example",       # suffix attack
    "http://evil.example#@testserver",       # fragment trick
    "http://testserver%2eevil.example",      # encoded dot
    "http://testserver.",                    # trailing dot is a different origin string
    "http://testserver:8080",                # port mismatch
    "ftp://testserver",
    "javascript:alert(1)",
    "https://evil.example/http://testserver",
    "http://TESTSERVER.evil.example",
]


# ════════════════════════════════════════════════════════════════════
# 1. F5/F7: hostile Origin / Referer matrix on every pre-session route
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("strict", [False, True], ids=["dev", "strict"])
def test_qa_hostile_origin_matrix_refused_with_no_side_effects(make_app, monkeypatch, strict):
    if strict:
        monkeypatch.setenv("VIDEO_FACTORY_REQUIRE_ORIGIN", "1")
    app, repo = make_app()
    c = TestClient(app)
    for origin in HOSTILE_ORIGINS:
        for name, (path, body) in PRE_SESSION_ROUTES.items():
            r = c.post(path, json=body, headers={"Origin": origin})
            assert r.status_code == 403, f"{name} accepted Origin={origin!r}: {r.status_code} {r.text[:120]}"
    # nothing was created / consumed by the refused calls
    assert repo.get_user_by_email("victim-qa@example.com") is None
    assert _count_rows(repo, "sessions") == 0
    assert _count_rows(repo, "password_resets") == 0
    assert _count_rows(repo, "rate_limit_hits") == 0, "refused requests must not burn rate-limit budget"


@pytest.mark.parametrize("referer", [
    "https://evil.example/page",
    "https://evil.example/?next=http://testserver/login",
    "https://evil.example/http://testserver/",
    "http://testserver.evil.example/login",
    "null",
    "not a url",
])
def test_qa_hostile_referer_only_refused(make_app, referer):
    app, repo = make_app()
    c = TestClient(app)
    for name, (path, body) in PRE_SESSION_ROUTES.items():
        r = c.post(path, json=body, headers={"Referer": referer})
        assert r.status_code == 403, f"{name}: Referer={referer!r} -> {r.status_code}"
    assert repo.get_user_by_email("victim-qa@example.com") is None


def test_qa_origin_wins_over_a_friendly_referer(make_app):
    """Hostile Origin + same-site Referer must still be refused."""
    app, _ = make_app()
    r = TestClient(app).post("/auth/forgot", json={"email": "a@example.com"},
                             headers={"Origin": "https://evil.example",
                                      "Referer": "http://testserver/forgot"})
    assert r.status_code == 403


def test_qa_legit_browser_shapes_not_blocked_in_strict_mode(make_app, monkeypatch):
    """Over-blocking guard: what real browsers send must keep working."""
    monkeypatch.setenv("VIDEO_FACTORY_ENV", "production")
    monkeypatch.setenv("VIDEO_FACTORY_PUBLIC_BASE_URL", "https://App.Example.com/")
    app, _ = make_app()
    c = TestClient(app, client=("127.0.0.1", 1))   # trusted proxy: one bucket per XFF client
    ok = [
        {"Origin": "http://testserver"},                              # own host
        {"Origin": "HTTP://TESTSERVER"},                              # case
        {"Origin": "http://testserver/"},                             # trailing slash
        {"Origin": "https://app.example.com"},                        # public base, normalised
        {"Origin": "https://app.example.com:443"},                    # explicit default port
        {"Referer": "http://testserver/login?x=1"},                   # Referer only
        {"Origin": "http://testserver", "host": "testserver:80"},     # Host with default port
    ]
    for i, h in enumerate(ok):
        h = {**h, "X-Forwarded-For": f"203.0.113.{i + 1}"}
        r = c.post("/auth/forgot", json={"email": f"nobody{i}@example.com"}, headers=h)
        assert r.status_code == 200, f"legit shape {h} refused: {r.status_code} {r.text}"


# ════════════════════════════════════════════════════════════════════
# 2. Internal token: no oracle, no confusion with CSRF tokens
# ════════════════════════════════════════════════════════════════════

def test_qa_internal_token_and_csrf_token_are_not_interchangeable(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_REQUIRE_ORIGIN", "1")
    app, repo = make_app()
    c = TestClient(app)
    r = _signup(c, "tok@example.com", headers={"X-VF-Internal-Token": _internal()})
    assert r.status_code == 200, r.text
    sid = _sid(r)
    # (a) the CSRF token of a real session is NOT an internal token
    r = c.post("/auth/forgot", json={"email": "x@example.com"},
               headers={"X-VF-Internal-Token": _csrf(sid)})
    assert r.status_code == 403
    # (b) the internal token is NOT a CSRF token for a real session
    r = c.post("/auth/logout", headers={**_ck(sid), "X-CSRF-Token": _internal()})
    assert r.status_code == 403
    assert _me(app, sid) == 200, "logout with the wrong CSRF token must not end the session"
    # (c) empty / whitespace-only header value is present-but-wrong -> 403, never a bypass
    for bad in ("", "   ", "\t"):
        r = c.post("/auth/forgot", json={"email": "x@example.com"},
                   headers={"X-VF-Internal-Token": bad})
        assert r.status_code == 403, repr(bad)


def test_qa_cookie_named_like_the_hmac_label_is_not_a_token_oracle(make_app):
    """internal token == HMAC(secret, 'vf-internal-caller:v1') == the CSRF token of a
    session whose id is that label. /auth/csrf derives a token from the cookie value, so
    it must never do so for an id that is not a live session."""
    app, _ = make_app()
    c = TestClient(app)
    for sid in ("vf-internal-caller:v1", "vf-internal-caller:v1 ", "../vf-internal-caller:v1"):
        r = c.get("/auth/csrf", headers=_ck(sid))
        assert r.status_code == 401, (sid, r.status_code, r.text)
        assert _internal() not in r.text


# ════════════════════════════════════════════════════════════════════
# 3. F8: session rotation, cross-user and failure variants
# ════════════════════════════════════════════════════════════════════

def test_qa_f8_cross_user_cookie_dropped_only_on_success(make_app):
    app, repo = make_app()
    alice = TestClient(app)
    ra = _signup(alice, "alice@example.com")
    sid_a = _sid(ra)
    rm = _signup(TestClient(app), "mallory@example.com")
    sid_m = _sid(rm)
    # failed login presenting Mallory's cookie must not revoke it
    r = _login(TestClient(app), "alice@example.com", "Wrong-Password-9", headers=_ck(sid_m))
    assert r.status_code == 401
    assert _me(app, sid_m) == 200
    # successful login presenting Mallory's cookie revokes THAT id and issues a new one
    r = _login(TestClient(app), "alice@example.com", headers=_ck(sid_m))
    assert r.status_code == 200
    new = _sid(r)
    assert new and new not in (sid_a, sid_m)
    assert _me(app, sid_m) == 401
    # Alice's other device (sid_a) survives: rotation is per presented id, not per user
    assert _me(app, sid_a) == 200
    assert _me(app, new) == 200
    # the new session's CSRF token is bound to the NEW id, not to the old one
    assert r.json()["csrf_token"] == _csrf(new) != _csrf(sid_m)


def test_qa_f8_parallel_logins_with_same_planted_cookie_do_not_500(make_app):
    app, repo = make_app()
    _signup(TestClient(app), "par@example.com")
    planted = _sid(_signup(TestClient(app), "other-par@example.com"))
    codes, sids = [], []
    lock = threading.Lock()

    def one():
        r = _login(TestClient(app), "par@example.com", headers=_ck(planted))
        with lock:
            codes.append(r.status_code)
            sids.append(_sid(r))
    ths = [threading.Thread(target=one) for _ in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert codes == [200] * 8, codes
    assert len(set(sids)) == 8, "every login must get its own fresh session id"
    assert _me(app, planted) == 401


def test_qa_f8_garbage_and_oversized_cookie_values_do_not_break_login(make_app):
    app, _ = make_app()
    _signup(TestClient(app), "garb@example.com")
    for val in ("", "'; DROP TABLE sessions;--", "A" * 5000, "%00", "ünïcode"):
        try:
            r = _login(TestClient(app), "garb@example.com", headers={"Cookie": f"vf_session={val}"})
        except UnicodeEncodeError:
            continue  # header not representable on the wire; not a server concern
        assert r.status_code == 200, (val[:20], r.status_code, r.text[:100])


# ════════════════════════════════════════════════════════════════════
# 4. F26: rate-limit and client-IP variants
# ════════════════════════════════════════════════════════════════════

def _burst_signup(app, client_addr, n, xff_for):
    c = TestClient(app, client=client_addr)
    codes = []
    for i in range(n):
        h = {"X-Forwarded-For": xff_for(i)} if xff_for else {}
        codes.append(_signup(c, f"b{secrets.token_hex(4)}{i}@example.com", headers=h).status_code)
    return codes


def test_qa_untrusted_peer_cannot_reset_bucket_by_rotating_xff(make_app):
    app, _ = make_app()
    codes = _burst_signup(app, ("198.51.100.7", 4000), 12, lambda i: f"203.0.113.{i + 1}")
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429], codes


def test_qa_trusted_proxy_xff_prepend_spoof_and_port_suffix_share_one_bucket(make_app):
    app, _ = make_app()
    real = "203.0.113.50"
    # junk prepended on the left, varying ports on the real hop
    codes = _burst_signup(app, ("127.0.0.1", 4000), 12,
                          lambda i: f"10.{i}.{i}.{i}, {real}:{1000 + i}")
    assert codes[10:] == [429, 429], codes


def test_qa_multiple_xff_header_lines_use_the_last_one(make_app):
    app, _ = make_app()
    c = TestClient(app, client=("127.0.0.1", 4000))
    codes = []
    for i in range(12):
        r = c.post("/auth/signup",
                   json={"email": f"ml{i}@example.com", "password": PASSWORD, "workspace_name": "M"},
                   headers=[("X-Forwarded-For", f"9.9.9.{i}"), ("X-Forwarded-For", "203.0.113.60")])
        codes.append(r.status_code)
    assert codes[10:] == [429, 429], codes


def test_qa_ipv4_mapped_ipv6_loopback_peer_is_trusted_like_loopback(make_app):
    app, _ = make_app()
    codes = _burst_signup(app, ("::ffff:127.0.0.1", 4000), 12, lambda i: "203.0.113.70")
    assert codes[10:] == [429, 429], codes
    # and a different real client behind the same proxy is not affected
    r = _signup(TestClient(app, client=("::ffff:127.0.0.1", 4000)), "free@example.com",
                headers={"X-Forwarded-For": "203.0.113.71"})
    assert r.status_code == 200


def test_qa_trusted_proxies_env_typo_does_not_trust_the_world(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_TRUSTED_PROXIES", "not-an-ip, 0/0/0, ")
    from auth import client_ip
    client_ip._parse_networks.cache_clear()
    try:
        app, _ = make_app()
        # loopback is no longer trusted (list is empty after dropping typos): XFF ignored
        codes = _burst_signup(app, ("127.0.0.1", 4000), 12, lambda i: f"203.0.113.{i + 1}")
        assert codes[10:] == [429, 429], codes
    finally:
        client_ip._parse_networks.cache_clear()


def test_qa_forgot_email_budget_is_case_and_space_insensitive_and_does_not_enumerate(make_app):
    app, repo = make_app()
    _signup(TestClient(app), "known@example.com")
    spellings = ["Known@Example.com", "  known@example.com", "KNOWN@EXAMPLE.COM", "known@example.com"]
    unknown = ["Ghost@Example.com", "  ghost@example.com", "GHOST@EXAMPLE.COM", "ghost@example.com"]
    got_known, got_unknown = [], []
    for i, (k, u) in enumerate(zip(spellings, unknown)):
        # a fresh client IP each time so only the e-mail bucket can trigger
        hk = {"X-Forwarded-For": f"203.0.113.{100 + i}"}
        hu = {"X-Forwarded-For": f"203.0.113.{200 + i}"}
        c = TestClient(app, client=("127.0.0.1", 1))
        rk = c.post("/auth/forgot", json={"email": k}, headers=hk)
        ru = c.post("/auth/forgot", json={"email": u}, headers=hu)
        got_known.append((rk.status_code, rk.json() if rk.status_code == 200 else None))
        got_unknown.append((ru.status_code, ru.json() if ru.status_code == 200 else None))
    assert got_known == got_unknown, (got_known, got_unknown)
    assert [s for s, _ in got_known] == [200, 200, 200, 429]
    # the limiter table holds digests, not addresses
    rows = repo._conn().execute("SELECT key FROM rate_limit_hits WHERE bucket='forgot_email'").fetchall()
    assert rows and all("@" not in r[0] and "example" not in r[0] for r in rows)


def test_qa_rate_limit_exactly_limit_under_parallel_load(make_app, monkeypatch):
    monkeypatch.setenv("VF_SIGNUP_IP_LIMIT", "7")
    app, _ = make_app()
    codes = []
    lock = threading.Lock()

    def one(i):
        r = _signup(TestClient(app, client=("198.51.100.99", 1)), f"pl{i}@example.com")
        with lock:
            codes.append(r.status_code)
    ths = [threading.Thread(target=one, args=(i,)) for i in range(25)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert sorted(set(codes)) == [200, 429], codes
    assert codes.count(200) == 7, f"limit 7 admitted {codes.count(200)}"


def test_qa_rate_limit_window_expires_and_denied_hits_are_not_recorded(make_app, monkeypatch):
    monkeypatch.setenv("VF_FORGOT_IP_LIMIT", "2")
    monkeypatch.setenv("VF_FORGOT_IP_WINDOW_SEC", "2")
    app, repo = make_app()
    c = TestClient(app, client=("198.51.100.5", 1))
    codes = [c.post("/auth/forgot", json={"email": f"w{i}@example.com"}).status_code for i in range(5)]
    assert codes == [200, 200, 429, 429, 429]
    assert _count_rows(repo, "rate_limit_hits", "bucket='forgot_ip'") == 2, "denied hits were recorded"
    time.sleep(4.5)   # hit_at has 1 s resolution: leave a wide margin
    assert c.post("/auth/forgot", json={"email": "after@example.com"}).status_code == 200


# ════════════════════════════════════════════════════════════════════
# 5. Reset-password flow regression under Origin + limits
# ════════════════════════════════════════════════════════════════════

def test_qa_password_reset_flow_survives_s1b_and_refused_calls_do_not_burn_the_token(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_REQUIRE_ORIGIN", "1")
    monkeypatch.setenv("VIDEO_FACTORY_DEV_EXPOSE_TOKENS", "1")   # dev only: returns reset_token
    app, repo = make_app()
    ok = {"Origin": "http://testserver"}
    c = TestClient(app)
    r = _signup(c, "reset@example.com", headers=ok)
    assert r.status_code == 200, r.text
    s1 = _sid(r)
    s2 = _sid(_login(TestClient(app), "reset@example.com", headers=ok))
    assert _me(app, s1) == _me(app, s2) == 200
    token = c.post("/auth/forgot", json={"email": "reset@example.com"}, headers=ok).json().get("reset_token")
    assert token, "dev opt-in should expose the reset token"
    body = {"token": token, "new_password": NEW_PASSWORD}
    # cross-site attempt with a VALID token: refused and the token stays usable
    r = c.post("/auth/forgot/consume", json=body, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert repo.get_password_reset(token)["consumed_at"] in (None, "")
    # legit consume
    r = c.post("/auth/forgot/consume", json=body, headers=ok)
    assert r.status_code == 200, r.text
    assert _me(app, s1) == 401 and _me(app, s2) == 401, "reset must revoke every session"
    assert _login(TestClient(app), "reset@example.com", PASSWORD, headers=ok).status_code == 401
    assert _login(TestClient(app), "reset@example.com", NEW_PASSWORD, headers=ok).status_code == 200
    # replay of the used token
    r = c.post("/auth/forgot/consume", json=body, headers=ok)
    assert r.status_code == 400


# ════════════════════════════════════════════════════════════════════
# 6. F24/F25: PKCE and state handling
# ════════════════════════════════════════════════════════════════════

_DISCOVERY = {
    "google": {"issuer": "https://accounts.google.com",
               "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
               "token_endpoint": "https://oauth2.googleapis.com/token",
               "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"},
    "github": {"issuer": "https://github.com",
               "authorization_endpoint": "https://github.com/login/oauth/authorize",
               "token_endpoint": "https://github.com/login/oauth/access_token",
               "jwks_uri": "https://github.com/.well-known/jwks"},
}


@pytest.fixture()
def oidc_app(make_app, monkeypatch):
    for p in ("GOOGLE", "GITHUB"):
        monkeypatch.setenv(f"{p}_OIDC_CLIENT_ID", f"{p.lower()}-client")
        monkeypatch.setenv(f"{p}_OIDC_CLIENT_SECRET", f"{p.lower()}-secret")
    monkeypatch.setenv("VIDEO_FACTORY_PUBLIC_BASE_URL", "http://testserver")
    import api.oidc as _oidc
    _oidc._reset_for_tests()

    def _fake_get(provider, *, force_refresh=False):
        return {"loaded_at": time.time(), "discovery": _DISCOVERY[provider],
                "jwks": {"keys": []}, "client_id": f"{provider}-client",
                "client_secret": f"{provider}-secret",
                "discovery_url": f"https://example/{provider}"}
    monkeypatch.setattr(_oidc._provider_cache, "get", _fake_get)
    app, repo = make_app()
    return app, repo, _oidc


def _start(c, provider="google"):
    r = c.get(f"/auth/oauth/{provider}/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    return r.headers["Location"], parse_qs(urlparse(r.headers["Location"]).query)


def _s256(v: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_qa_pkce_challenge_matches_rfc7636_appendix_b():
    from api.oidc import pkce_challenge
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_qa_pkce_flows_are_isolated_and_state_is_single_use(oidc_app, monkeypatch):
    """Two parallel OAuth /start calls in different browsers must each
    deliver their OWN code_verifier (never cross) on their callback —
    and replay of a consumed state must be refused before any code exchange.

    Note on the two-client split: under S1b-01 the ``oa_state`` cookie
    binds the callback to the browser that called /start. A single
    ``TestClient`` calling /start twice in a row would only carry the
    LAST set cookie; that breaks the second callback here. We use two
    separate clients (each with its own cookie jar) — exactly mirroring
    two browsers on one machine.
    """
    app, repo, _oidc = oidc_app
    c1 = TestClient(app)
    c2 = TestClient(app)
    url1, qs1 = _start(c1)
    url2, qs2 = _start(c2)
    assert qs1["code_challenge"] != qs2["code_challenge"] and qs1["state"] != qs2["state"]
    rows = {s: v for s, v in repo._conn().execute("SELECT state, code_verifier FROM oidc_pending")}
    v1, v2 = rows[qs1["state"][0]], rows[qs2["state"][0]]
    assert v1 != v2
    for v in (v1, v2):                      # the verifier never appears in any redirect URL
        assert v not in url1 and v not in url2
    seen = []

    def _rec(*, provider, code, redirect_uri, code_verifier=None):
        seen.append(code_verifier)
        raise _oidc.OIDCCallbackError("stop")
    monkeypatch.setattr(_oidc, "_exchange_code", _rec)
    # flow 2 first (browser 2 has qs2's oa_state cookie): must deliver v2, not v1
    assert c2.get(f"/auth/oauth/google/callback?code=c&state={qs2['state'][0]}").status_code == 400
    assert seen == [v2]
    # replay of the consumed state in the same browser: refused BEFORE any code exchange
    r = c2.get(f"/auth/oauth/google/callback?code=c&state={qs2['state'][0]}")
    assert r.status_code == 400 and seen == [v2]
    # the sibling flow in browser 1 is still alive and uses its own verifier
    assert c1.get(f"/auth/oauth/google/callback?code=c&state={qs1['state'][0]}").status_code == 400
    assert seen == [v2, v1]


def test_qa_expired_state_never_reaches_the_token_endpoint(oidc_app, monkeypatch):
    app, repo, _oidc = oidc_app
    c = TestClient(app)
    _, qs = _start(c)
    repo._conn().execute("UPDATE oidc_pending SET expires_at='2001-01-01T00:00:00Z' WHERE state=?",
                         (qs["state"][0],))
    repo._conn().commit()
    called = []
    monkeypatch.setattr(_oidc, "_exchange_code", lambda **kw: called.append(kw) or {})
    assert c.get(f"/auth/oauth/google/callback?code=c&state={qs['state'][0]}").status_code == 400
    assert called == []


def test_qa_state_minted_for_one_provider_is_not_redeemable_at_the_other(oidc_app, monkeypatch):
    """State + PKCE verifier from the Google start must not reach the GitHub token exchange."""
    app, repo, _oidc = oidc_app
    c = TestClient(app, raise_server_exceptions=False)
    _, qs = _start(c, "google")
    called = []

    def _rec(*, provider, code, redirect_uri, code_verifier=None):
        called.append(provider)
        raise _oidc.OIDCCallbackError("stop")
    monkeypatch.setattr(_oidc, "_exchange_code", _rec)
    r = c.get(f"/auth/oauth/github/callback?code=c&state={qs['state'][0]}")
    assert r.status_code == 400, r.text
    assert called == [], f"a Google-minted state reached the {called} token exchange"


def test_qa_oidc_callback_requires_the_browser_that_started_the_flow(oidc_app, monkeypatch):
    """QA-S1b-01: the OAuth state must be bound to the browser that
    called /start — a callback in a different browser must be refused.

    Three sub-scenarios in one test:
      1. The browser that called /start has the correct ``oa_state``
         cookie and gets a normal 200 (happy path) — the response
         also clears the cookie so it cannot be replayed.
      2. A different browser with NO ``oa_state`` cookie (the
         victim in a CSRF) hits the callback URL → 400, no session
         issued, no token exchange performed.
      3. A browser with a present-but-wrong ``oa_state`` cookie
         (a guessed/forged value) hits the callback → 400 as well.
    """
    app, repo, _oidc = oidc_app
    import httpx as _hx

    # The /start route lands an ``oa_state`` cookie on the calling
    # browser that is HMAC-SHA256(server_secret, "oa_state:v1:"+state).
    def _cookie_present(c):
        jar = c.cookies
        return ("oa_state" in jar) and bool(jar.get("oa_state"))

    # Helper: stub the network so the callback would succeed if
    # it were going to succeed — anything claiming a verified
    # primary e-mail gets routed through.
    def _stub_legit_legit_email(monkeypatch, *, email, sub):
        monkeypatch.setattr(_oidc, "_exchange_code",
                            lambda **kw: {"access_token": "tok"})

        class _R:
            def __init__(self, data):
                self._d = data
                self.status_code = 200

            def json(self):
                return self._d

        def _get(url, **kw):
            if url.endswith("/user"):
                return _R({"id": sub, "login": "legit"})
            return _R([{"email": email, "primary": True, "verified": True}])

        monkeypatch.setattr(_hx, "get", _get)

    # ── (1) Happy path: same browser, correct cookie, succeeds ──
    legit_browser = TestClient(app)
    r_start = legit_browser.get("/auth/oauth/github/start",
                                follow_redirects=False)
    assert r_start.status_code == 302, r_start.text
    state_blob = parse_qs(urlparse(r_start.headers["Location"]).query)["state"][0]
    assert _cookie_present(legit_browser), (
        "/start must land an oa_state cookie on the calling browser; "
        f"cookies seen: {list(legit_browser.cookies)}")
    _stub_legit_legit_email(monkeypatch, email="legit-owner@example.com",
                            sub=424242)
    # State is single-use: do the callback exactly once and let the
    # TestClient follow whatever landing page the auth flow lands on.
    r_ok = legit_browser.get(
        f"/auth/oauth/github/callback?code=c&state={state_blob}",
        follow_redirects=True,
    )
    assert r_ok.status_code == 200, (
        f"legit browser with correct cookie was refused: "
        f"{r_ok.status_code} {r_ok.text[:200]}")
    # The /callback middleware clears the ``oa_state`` cookie on
    # every exit — success or error — so it cannot be replayed.
    assert not legit_browser.cookies.get("oa_state"), (
        "oa_state cookie must be cleared after the callback; "
        f"jar is {list(legit_browser.cookies)}")

    # ── (2) Victim browser: NO cookie → 400 ──
    attacker_browser = TestClient(app)
    victim_browser = TestClient(app)
    r_attacker = attacker_browser.get("/auth/oauth/github/start",
                                      follow_redirects=False)
    assert r_attacker.status_code == 302, r_attacker.text
    qs = parse_qs(urlparse(r_attacker.headers["Location"]).query)
    attacker_state = qs["state"][0]
    # Confirm the attacker DOES have the cookie (we want a sharp
    # assertion that the victim does NOT).
    assert _cookie_present(attacker_browser), (
        "attacker should have oa_state set; "
        f"jar={list(attacker_browser.cookies)}")
    called = []
    monkeypatch.setattr(_oidc, "_exchange_code",
                        lambda **kw: called.append(kw) or {"access_token": "tok"})

    class _R2:
        def __init__(self, data):
            self._d = data
            self.status_code = 200

        def json(self):
            return self._d

    def _get2(url, **kw):
        if url.endswith("/user"):
            return _R2({"id": 777001, "login": "attacker"})
        return _R2([{"email": "attacker-owned@example.com",
                     "primary": True, "verified": True}])

    monkeypatch.setattr(_hx, "get", _get2)
    # Victim browser has its own cookie jar separate from the
    # attacker's, exactly mirroring two real browsers sharing one
    # machine. The victim must NOT be logged in as the attacker.
    r_victim = victim_browser.get(
        f"/auth/oauth/github/callback?code=attacker-code&state={attacker_state}",
        follow_redirects=False,
    )
    assert r_victim.status_code == 400, (
        f"victim browser must be refused with 400, got "
        f"{r_victim.status_code}: {r_victim.text[:200]}")
    assert called == [], (
        f"victim browser reached the token exchange: "
        f"the attacker-minted callback would have signed the victim in: {called}")
    # No attacker session is on the victim jar.
    assert victim_browser.cookies.get("vf_session") is None, (
        "victim browser must not receive a vf_session cookie")

    # ── (3) Garbage cookie value → 400 ──
    # A guessing attacker can plant a cookie in the victim's
    # browser — but a wrong value must still be refused.
    guesser_browser = TestClient(app)
    # Re-issue fresh /start on a separate client to obtain a
    # legit state the guesser will try to pair with the wrong cookie.
    fresh_attacker = TestClient(app)
    r_fresh = fresh_attacker.get("/auth/oauth/github/start",
                                 follow_redirects=False)
    state_for_guesser = parse_qs(
        urlparse(r_fresh.headers["Location"]).query)["state"][0]
    guesser_browser.cookies.set("oa_state", "0" * 64)   # right shape, wrong value
    called2 = []
    monkeypatch.setattr(_oidc, "_exchange_code",
                        lambda **kw: called2.append(kw) or {"access_token": "tok"})
    r_guess = guesser_browser.get(
        f"/auth/oauth/github/callback?code=c&state={state_for_guesser}",
        follow_redirects=False,
    )
    assert r_guess.status_code == 400, (
        f"callback with wrong cookie value must be 400, got "
        f"{r_guess.status_code}: {r_guess.text[:200]}")
    assert called2 == [], (
        f"wrong-cookie callback reached the token exchange: {called2}")


@pytest.mark.parametrize("provider,expected_scope", [("google", ["openid email profile"]),
                                                      ("github", ["user:email"])])
def test_qa_authorize_url_has_exactly_one_challenge_and_s256_only(oidc_app, provider, expected_scope):
    app, _, _ = oidc_app
    url, qs = _start(TestClient(app), provider)
    assert qs["scope"] == expected_scope
    assert qs["code_challenge_method"] == ["S256"] and len(qs["code_challenge"]) == 1
    assert len(qs["code_challenge"][0]) == 43            # base64url(sha256) without padding
    assert url.count("code_challenge=") == 1 and "plain" not in url
    assert "client_secret" not in url and "code_verifier" not in url


# ════════════════════════════════════════════════════════════════════
# 7. F29: session-secret rotation matrix
# ════════════════════════════════════════════════════════════════════

def test_qa_previous_secret_list_semantics(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET_PREVIOUS", f" {OLD_SECRET} , ,, second-old-secret ")
    app, repo = make_app()
    c = TestClient(app)
    r = _signup(c, "rot@example.com")
    sid = _sid(r)
    # tokens issued now are signed with the CURRENT secret only
    assert r.json()["csrf_token"] == _csrf(sid)
    assert r.json()["csrf_token"] != _csrf(sid, OLD_SECRET)
    # verify-only: tokens signed with the current OR any listed previous secret are accepted
    for sec in (SECRET, OLD_SECRET, "second-old-secret"):
        s = _sid(_login(TestClient(app), "rot@example.com"))
        rr = TestClient(app).post("/auth/logout", headers={**_ck(s), "X-CSRF-Token": _csrf(s, sec)})
        assert rr.status_code == 200, (sec[:12], rr.status_code, rr.text)
        assert _me(app, s) == 401
    # an unlisted secret and the EMPTY key are both refused
    s = _sid(_login(TestClient(app), "rot@example.com"))
    for bad in ("never-listed-secret", ""):
        tok = hmac.new(bad.encode(), s.encode(), hashlib.sha256).hexdigest()
        rr = TestClient(app).post("/auth/logout", headers={**_ck(s), "X-CSRF-Token": tok})
        assert rr.status_code == 403, (bad, rr.status_code)
    assert _me(app, s) == 200


def test_qa_previous_secret_removed_means_old_tokens_die(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET_PREVIOUS", OLD_SECRET)
    app, _ = make_app()
    s = _sid(_signup(TestClient(app), "gone@example.com"))
    old_tok = _csrf(s, OLD_SECRET)
    assert TestClient(app).post("/auth/logout", headers={**_ck(s), "X-CSRF-Token": "0" * 64}).status_code == 403
    monkeypatch.delenv("VIDEO_FACTORY_SESSION_SECRET_PREVIOUS")
    assert TestClient(app).post("/auth/logout", headers={**_ck(s), "X-CSRF-Token": old_tok}).status_code == 403
    assert TestClient(app).post("/auth/logout", headers={**_ck(s), "X-CSRF-Token": _csrf(s)}).status_code == 200


def test_qa_previous_secret_equal_to_current_adds_nothing_and_internal_token_follows_rule(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET_PREVIOUS", SECRET)
    monkeypatch.setenv("VIDEO_FACTORY_REQUIRE_ORIGIN", "1")
    app, _ = make_app()
    c = TestClient(app)
    assert c.post("/auth/forgot", json={"email": "a@example.com"},
                  headers={"X-VF-Internal-Token": _internal()}).status_code == 200
    assert c.post("/auth/forgot", json={"email": "a@example.com"},
                  headers={"X-VF-Internal-Token": _internal(OLD_SECRET)}).status_code == 403
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET_PREVIOUS", OLD_SECRET)
    assert c.post("/auth/forgot", json={"email": "b@example.com"},
                  headers={"X-VF-Internal-Token": _internal(OLD_SECRET)}).status_code == 200


# ════════════════════════════════════════════════════════════════════
# 8. F11/F15: key rotation endpoint, odd callers and odd keys
# ════════════════════════════════════════════════════════════════════

OLD_KEY = "qa-old-db-key-" + "A" * 24
NEW_KEY = "qa-new-db-key-" + "B" * 24


def _owner(app, email="owner@example.com"):
    r = _signup(TestClient(app), email)
    assert r.status_code == 200, r.text
    return _sid(r), r.json()["user_id"]


def _platform_admin(app, email="admin@example.com"):
    """Sign up a user and add them to VIDEO_FACTORY_PLATFORM_ADMINS.

    Returns (client, user_id, sid).  Mirrors the helper from
    tests/test_security_s1b.py — duplicated here so the QA
    regression file stays self-contained and can run with a
    narrower pytest collection (no S1b dependency).
    """
    import os as _os
    c = TestClient(app)
    r = _signup(c, email=email)
    assert r.status_code == 200, r.text
    uid = r.json()["user_id"]
    sid = _sid(r)
    existing = _os.environ.get("VIDEO_FACTORY_PLATFORM_ADMINS", "")
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    if uid not in parts:
        parts.append(uid)
    _os.environ["VIDEO_FACTORY_PLATFORM_ADMINS"] = ",".join(parts)
    return c, uid, sid


def _rotate(app, sid, body, *, csrf=True, raw=None):
    h = _ck(sid)
    if csrf:
        h["X-CSRF-Token"] = _csrf(sid)
    if raw is not None:
        return TestClient(app).post("/api/v1/admin/keys/rotate", headers={**h, "content-type": "application/json"},
                                    content=raw)
    return TestClient(app).post("/api/v1/admin/keys/rotate", headers=h, content=json.dumps(body))


def _audit_text(repo) -> str:
    out = []
    for t in ("audit_log", "runner_audit", "key_rotation"):
        try:
            out += [str(tuple(r)) for r in repo._conn().execute(f"SELECT * FROM {t}").fetchall()]
        except Exception:
            pass
    return "\n".join(out)


@pytest.mark.parametrize("bad_pw", [None, 12345, True, ["x"], {"p": 1}, "", " "])
def test_qa_rotate_non_string_or_blank_password_is_403_not_500(make_app, bad_pw):
    app, _ = make_app(db_key=OLD_KEY)
    sid, _ = _owner(app)
    r = _rotate(app, sid, {"old_key": OLD_KEY, "new_key": NEW_KEY, "current_password": bad_pw})
    assert r.status_code == 403, (bad_pw, r.status_code, r.text)


def test_qa_rotate_oidc_only_account_without_password_cannot_reauth(make_app):
    app, repo = make_app(db_key=OLD_KEY)
    sid, uid = _owner(app)
    repo._conn().execute("UPDATE users SET password_hash='' WHERE user_id=?", (uid,))
    repo._conn().commit()
    for pw in (PASSWORD, "", "anything"):
        r = _rotate(app, sid, {"old_key": OLD_KEY, "new_key": NEW_KEY, "current_password": pw})
        assert r.status_code in (403, 429), (pw, r.status_code, r.text)
    assert repo.db_key == OLD_KEY, "rotation ran for an account that has no password"


def test_qa_rotate_viewer_only_member_refused_before_any_key_handling(make_app, caplog):
    caplog.set_level(logging.DEBUG)
    app, repo = make_app(db_key=OLD_KEY)
    sid, uid = _owner(app)
    repo._conn().execute("UPDATE memberships SET role='viewer' WHERE user_id=?", (uid,))
    repo._conn().commit()
    r = _rotate(app, sid, {"old_key": OLD_KEY, "new_key": NEW_KEY, "current_password": PASSWORD})
    assert r.status_code == 403
    assert OLD_KEY not in caplog.text and NEW_KEY not in caplog.text
    assert repo.db_key == OLD_KEY


def test_qa_rotate_csrf_checked_before_password_so_wrong_pw_cannot_probe_without_csrf(make_app):
    app, repo = make_app(db_key=OLD_KEY)
    sid, uid = _owner(app)
    before = _count_rows(repo, "rate_limit_hits", "bucket='keys_reauth'")
    r = _rotate(app, sid, {"old_key": OLD_KEY, "new_key": NEW_KEY, "current_password": PASSWORD}, csrf=False)
    assert r.status_code == 403
    assert _count_rows(repo, "rate_limit_hits", "bucket='keys_reauth'") == before, \
        "a CSRF-less request must not even touch the re-auth budget / password check"


def test_qa_rotate_special_character_keys_never_leak_even_in_escaped_form(make_app, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    odd_old = "o'ld\"key;--é" + "Q" * 20
    odd_new = "n'ew\\key\u00fc'" + "Z" * 20
    wrong_old = "wr'ong\"old-key" + "W" * 18
    app, repo = make_app(db_key=odd_old)
    sid, _ = _owner(app)
    blobs = []
    r = _rotate(app, sid, {"old_key": wrong_old, "new_key": odd_new, "current_password": PASSWORD})
    assert r.status_code in (400, 500, 503), r.text
    blobs.append(r.text)
    r = _rotate(app, sid, {"old_key": odd_old, "new_key": odd_new, "current_password": PASSWORD})
    assert r.status_code == 200, r.text
    blobs.append(r.text)
    assert repo.db_key == odd_new
    assert _signup(TestClient(app), "after-rotation@example.com").status_code == 200, "repo unusable after rotation"
    out = capsys.readouterr()
    blobs += [caplog.text, out.out, out.err, _audit_text(repo)]
    for k in (odd_old, odd_new, wrong_old):
        for form in (k, k.replace("'", "''"), k.replace("\\", "\\\\")):
            for b in blobs:
                assert form not in b, f"key material {form[:8]}... leaked"


def test_qa_wrong_key_rotation_attempt_by_ordinary_tenant_does_not_touch_global_rotation_state(make_app):
    """A wrong-old-key attempt must NOT flip the global ``key_rotation.failed``.

    Background: the endpoint re-keys the WHOLE multi-tenant DB.  An
    ordinary tenant (no admin role) hitting ``/api/v1/admin/keys/rotate``
    with a wrong ``old_key`` used to:

      (1) survive the (broken) role check,
      (2) flip ``key_rotation.failed = 1`` in the catch-all handler,
      (3) emit a 500 audit row ``key.rotation_failed`` (infra-fault shape).

    Reserved infra-fault rows are reserved for real incidents
    (corruption, IO error, migration crash).  A caller error must
    stay out of that bucket.  This test pins the contract:

      * HTTP 403 (admin gate is in scope on this endpoint, OR the
        caller-error path inside ``rotate()`` surfaces 403 to
        platform-admin callers that supplied a wrong ``old_key``),
      * ``key_rotation.failed == False``,
      * ``key.rotation_failed`` row NOT written (infra-fault event
        reserved for real incidents — never for caller error),
      * If the rotation actually ran (i.e. the caller was a
        platform-admin and the BadOldKey branch inside ``rotate()``
        fired), a ``key.rotation_rejected_caller_error`` row IS
        written; if the admin gate short-circuited the request no
        rotation row is written at all — both shapes are
        acceptable, the only forbidden shape is the infra-fault
        event on a caller-error path.
    """
    from control_plane.key_rotation import rotation_state
    app, repo = make_app(db_key=OLD_KEY)
    sid, _ = _owner(app, "tenant-b@example.com")
    r = _rotate(app, sid, {"old_key": "wrong-" + OLD_KEY, "new_key": NEW_KEY,
                           "current_password": PASSWORD})
    # Caller-error / forbidden path: 403 in either shape (admin gate
    # short-circuit, or BadOldKey inside rotate()).
    assert r.status_code == 403, r.text
    # Global flag MUST stay untouched — this is the alarm reserved
    # for real infrastructure faults.
    state = rotation_state(repo)
    assert state["failed"] is False, (
        "a wrong-old-key attempt flipped the platform-wide "
        "key_rotation.failed flag — caller-error must stay out "
        "of the infra-fault alarm path"
    )
    assert state["in_progress"] is False, (
        "rotation_in_progress must be cleared after a caller-error "
        "rejection so the API is usable again"
    )
    # Audit row shape: the infra-fault event MUST NOT be emitted on
    # a caller-error path.  The caller-error event
    # ``key.rotation_rejected_caller_error`` is only written when
    # the rotation actually ran and BadOldKey fired inside rotate();
    # if the admin gate short-circuited, no rotation row is
    # written at all — both shapes are acceptable.
    failed_rows = _count_rows(
        repo, "runner_audit", "event='key.rotation_failed'",
    )
    assert failed_rows == 0, (
        "key.rotation_failed was emitted on a caller-error path — "
        "this row is reserved for real infrastructure faults "
        "(corruption, IO error, migration crash)"
    )


def test_qa_platform_admin_wrong_old_key_surfaces_403_without_flipping_global_flag(make_app):
    """A platform-admin caller with a wrong ``old_key`` must see 403, not 500.

    This is the BadOldKey path inside ``rotate()``: the admin gate
    lets the caller through (they are in VIDEO_FACTORY_PLATFORM_ADMINS),
    the rotation code reaches ``_table_counts(db_path, key=old_key)``
    or ``copy_cipher_to_cipher(src_key=old_key, ...)``, SQLCipher
    raises a ``DatabaseError`` ("file is not a database"), and the
    translator converts it into a :class:`BadOldKey` so the broad
    ``except Exception`` block does NOT flip ``rotation_failed = 1``.

    Contract:

      * HTTP 403 (caller-error path; admin gate is in scope),
      * ``key_rotation.failed == False``,
      * ``key.rotation_failed`` row NOT written,
      * ``key.rotation_rejected_caller_error`` row IS written,
      * ``rotation_in_progress == False`` (cleared so the API
        is usable again — the operator can retry with the right
        key without restart).
    """
    import os as _os
    from control_plane.key_rotation import rotation_state
    app, repo = make_app(db_key=OLD_KEY)
    c, uid, sid = _platform_admin(app, email="pa@example.com")
    # Sanity: the platform-admin allow-list contains this user.
    assert uid in _os.environ.get("VIDEO_FACTORY_PLATFORM_ADMINS", "")
    r = _rotate(app, sid, {"old_key": "wrong-" + OLD_KEY, "new_key": NEW_KEY,
                           "current_password": PASSWORD})
    # Caller-error path → 403 from the handler.
    assert r.status_code == 403, r.text
    # Global flag MUST stay untouched.
    state = rotation_state(repo)
    assert state["failed"] is False, (
        "a platform-admin wrong-old-key attempt flipped the "
        "platform-wide key_rotation.failed flag — caller-error "
        "must stay out of the infra-fault alarm path"
    )
    assert state["in_progress"] is False, (
        "rotation_in_progress must be cleared after BadOldKey so "
        "the API is usable again"
    )
    # Audit shape: caller-error event MUST be present; infra-fault
    # event MUST NOT be present.
    assert _count_rows(
        repo, "runner_audit",
        "event='key.rotation_rejected_caller_error'",
    ) >= 1, "no caller-error audit row was written for the BadOldKey path"
    assert _count_rows(
        repo, "runner_audit", "event='key.rotation_failed'",
    ) == 0, (
        "key.rotation_failed was emitted on a BadOldKey path — "
        "infra-fault events must never leak from caller-error"
    )


def test_qa_rotate_oversized_and_deeply_nested_bodies_are_400_not_500(make_app):
    app, _ = make_app(db_key=OLD_KEY)
    sid, _ = _owner(app)
    for raw in ("[" * 500 + "]" * 500, '{"old_key":' * 50, '"' + "A" * 200_000 + '"', "\x00\xff"):
        r = _rotate(app, sid, None, raw=raw)
        assert r.status_code in (400, 413), (raw[:20], r.status_code, r.text[:100])
        assert "AAAA" not in r.text


def test_qa_documented_rotation_flow_in_operations_doc_works_end_to_end(make_app):
    """docs/operations-prod.md: GET /auth/csrf with the cookie, then POST with X-CSRF-Token
    and current_password in a JSON body. Executes the documented steps literally."""
    app, repo = make_app(db_key=OLD_KEY)
    sid, _ = _owner(app)
    c = TestClient(app)
    csrf = c.get("/auth/csrf", headers=_ck(sid)).json()["csrf_token"]
    r = c.post("/api/v1/admin/keys/rotate", headers={**_ck(sid), "X-CSRF-Token": csrf,
                                                     "Content-Type": "application/json"},
               content=json.dumps({"old_key": OLD_KEY, "new_key": NEW_KEY, "current_password": PASSWORD}))
    assert r.status_code == 200, r.text
    assert r.json()["completed"] is True and r.json()["new_key_fingerprint"] == hashlib.sha256(NEW_KEY.encode()).hexdigest()[:16]
    assert c.get("/api/v1/admin/keys/status", headers=_ck(sid)).json()["completed"] is True


# ════════════════════════════════════════════════════════════════════
# 9. F12: webhook replay, atomicity, unsigned-header tamper
# ════════════════════════════════════════════════════════════════════

def _sig(body: bytes, secret="hg-qa-secret"):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_qa_webhook_replay_with_tampered_unsigned_event_header_is_still_a_duplicate(make_app, monkeypatch):
    monkeypatch.setenv("HEYGEN_WEBHOOK_SECRET", "hg-qa-secret")
    app, repo = make_app()
    calls = []
    monkeypatch.setattr(repo, "list_jobs", lambda **kw: [{"job_id": "job_q", "provider_job_id": "v_q"}])
    monkeypatch.setattr(repo, "update_job", lambda job_id, **f: calls.append(f))
    body = b'{"event_type":"video.completed","video_id":"v_q"}'
    c = TestClient(app)
    assert c.post("/api/v1/webhooks/heygen", content=body, headers={"X-Heygen-Signature": _sig(body)}).status_code == 200
    n = len(calls)
    # the event header is NOT covered by the signature: an attacker can flip it freely
    r = c.post("/api/v1/webhooks/heygen", content=body,
               headers={"X-Heygen-Signature": _sig(body), "X-Heygen-Event": "video.failed"})
    assert r.status_code == 200 and r.json().get("duplicate") is True
    # signature header spelling variants must not defeat the dedup either
    for sig in (_sig(body).upper().replace("SHA256=", "sha256="), _sig(body)[len("sha256="):]):
        r = c.post("/api/v1/webhooks/heygen", content=body, headers={"X-Heygen-Signature": sig})
        assert r.status_code in (200, 401)
        if r.status_code == 200:
            assert r.json().get("duplicate") is True
    assert len(calls) == n, "replay changed job state"


def test_qa_webhook_concurrent_replays_process_exactly_once(make_app, monkeypatch):
    monkeypatch.setenv("HEYGEN_WEBHOOK_SECRET", "hg-qa-secret")
    app, repo = make_app()
    calls = []
    monkeypatch.setattr(repo, "list_jobs", lambda **kw: [{"job_id": "job_c", "provider_job_id": "v_c"}])
    monkeypatch.setattr(repo, "update_job", lambda job_id, **f: calls.append(f))
    body = b'{"event_type":"video.failed","video_id":"v_c"}'
    results = []
    lock = threading.Lock()

    def one():
        r = TestClient(app).post("/api/v1/webhooks/heygen", content=body, headers={"X-Heygen-Signature": _sig(body)})
        with lock:
            results.append((r.status_code, bool(r.json().get("duplicate"))))
    ths = [threading.Thread(target=one) for _ in range(16)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert all(s == 200 for s, _ in results), results
    assert sum(1 for _, d in results if not d) == 1, results
    assert len(calls) == 1


def test_qa_webhook_unsigned_or_badly_signed_bodies_never_create_dedup_rows(make_app, monkeypatch):
    monkeypatch.setenv("HEYGEN_WEBHOOK_SECRET", "hg-qa-secret")
    app, repo = make_app()
    c = TestClient(app)
    body = b'{"event_type":"video.completed","video_id":"v_n"}'
    for h in ({}, {"X-Heygen-Signature": "sha256=" + "0" * 64}, {"X-Heygen-Signature": _sig(body, "other")}):
        assert c.post("/api/v1/webhooks/heygen", content=body, headers=h).status_code == 401
    assert _count_rows(repo, "webhook_receipts") == 0, "unauthenticated request poisoned the dedup table"
    # the genuine delivery after the junk is still processed (not shadowed as a duplicate)
    r = c.post("/api/v1/webhooks/heygen", content=body, headers={"X-Heygen-Signature": _sig(body)})
    assert r.status_code == 200 and not r.json().get("duplicate")
    # signed but not JSON: 400, and not recorded
    junk = b"not json at all"
    assert c.post("/api/v1/webhooks/heygen", content=junk, headers={"X-Heygen-Signature": _sig(junk)}).status_code == 400
    assert _count_rows(repo, "webhook_receipts") == 1


def test_qa_webhook_dedup_rows_older_than_retention_are_pruned_and_redelivery_accepted(make_app, monkeypatch):
    monkeypatch.setenv("HEYGEN_WEBHOOK_SECRET", "hg-qa-secret")
    monkeypatch.setenv("HEYGEN_WEBHOOK_DEDUP_DAYS", "1")
    app, repo = make_app()
    c = TestClient(app)
    body = b'{"event_type":"video.completed","video_id":"v_old"}'
    h = {"X-Heygen-Signature": _sig(body)}
    assert c.post("/api/v1/webhooks/heygen", content=body, headers=h).json().get("duplicate") is None
    repo._conn().execute("UPDATE webhook_receipts SET received_at='2001-01-01T00:00:00Z'")
    repo._conn().commit()
    r = c.post("/api/v1/webhooks/heygen", content=body, headers=h)
    assert r.status_code == 200 and not r.json().get("duplicate"), "retention is documented; expired rows must not dedup"


# ════════════════════════════════════════════════════════════════════
# 10. F14 / F19: CORS and security headers
# ════════════════════════════════════════════════════════════════════

def _acao(app, origin):
    r = TestClient(app).options("/auth/login", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-csrf-token"})
    return r.headers.get("access-control-allow-origin"), r


@pytest.mark.parametrize("env_value", ["*", " * ", "null", "javascript:alert(1)", "evil.example", "ftp://evil.example", ",,,"])
def test_qa_cors_junk_entries_never_open_the_api(make_app, monkeypatch, env_value):
    monkeypatch.setenv("VIDEO_FACTORY_CORS_ORIGINS", env_value)
    app, _ = make_app()
    for origin in ("https://evil.example", "null", "http://testserver"):
        acao, r = _acao(app, origin)
        assert acao in (None,), (env_value, origin, acao)


def test_qa_cors_mixed_list_allows_only_the_explicit_origin_and_no_credentials(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_CORS_ORIGINS", "*, https://Good.Example/, null")
    app, _ = make_app()
    acao, r = _acao(app, "https://good.example")
    assert acao == "https://good.example"
    assert r.headers.get("access-control-allow-credentials") is None
    assert _acao(app, "https://evil.example")[0] is None
    assert _acao(app, "https://good.example.evil.example")[0] is None
    assert "x-vf-internal-token" not in (r.headers.get("access-control-allow-headers") or "").lower()
    # explicit kwarg with '*' is dropped as well
    app2, _ = make_app(cors_origins=["*"])
    assert _acao(app2, "https://evil.example")[0] is None


def test_qa_security_headers_on_crash_head_stream_and_preflight(make_app, monkeypatch):
    from fastapi.responses import StreamingResponse, Response
    monkeypatch.setenv("VIDEO_FACTORY_CORS_ORIGINS", "https://good.example")
    app, _ = make_app()

    @app.get("/_qa/boom")
    async def _boom():
        raise RuntimeError("qa crash with secret-ish text")

    @app.get("/_qa/stream")
    async def _stream():
        def gen():
            yield b"chunk-1"
            yield b"chunk-2"
        return StreamingResponse(gen(), media_type="video/mp4")

    @app.get("/_qa/own-xfo")
    async def _own():
        return Response("x", headers={"X-Frame-Options": "SAMEORIGIN"})

    want = {"x-content-type-options": "nosniff", "x-frame-options": "DENY",
            "content-security-policy": "frame-ancestors 'none'", "referrer-policy": "no-referrer"}
    c = TestClient(app, raise_server_exceptions=False)
    cases = {
        "crash": c.get("/_qa/boom"), "stream": c.get("/_qa/stream"), "head": c.head("/health"),
        "404": c.get("/nope"), "405": c.delete("/health"),
        "preflight_ok": c.options("/auth/login", headers={"Origin": "https://good.example",
                                                         "Access-Control-Request-Method": "POST"}),
        "preflight_denied": c.options("/auth/login", headers={"Origin": "https://evil.example",
                                                             "Access-Control-Request-Method": "POST"}),
    }
    assert cases["crash"].status_code == 500
    assert "qa crash" not in cases["crash"].text
    assert cases["stream"].content == b"chunk-1chunk-2"
    for name, r in cases.items():
        for h, v in want.items():
            assert r.headers.get(h) == v, f"{name}: {h}={r.headers.get(h)!r}"
            assert len(r.headers.get_list(h)) == 1, f"{name}: duplicated {h}"
    # a route that sets its own header keeps it (middleware never overrides)
    assert c.get("/_qa/own-xfo").headers.get("x-frame-options") == "SAMEORIGIN"


# ════════════════════════════════════════════════════════════════════
# 11. A2 F-05: Secure cookie matrix
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("env,secure", [
    ("production", True), ("PRODUCTION", True), (" prod ", True), ("Staging", True),
    ("dev", False), ("development", False), ("", False), ("test", False),
])
def test_qa_secure_cookie_by_env_value(make_app, monkeypatch, env, secure):
    monkeypatch.setenv("VIDEO_FACTORY_ENV", env)
    monkeypatch.setenv("VIDEO_FACTORY_REQUIRE_ORIGIN", "0")
    app, _ = make_app()
    r = _signup(TestClient(app), "ck@example.com")
    sc = r.headers.get("set-cookie", "")
    assert ("Secure" in sc) is secure, (env, sc)
    assert "HttpOnly" in sc and "SameSite=Strict" in sc and "Path=/" in sc


def test_qa_clear_cookie_on_logout_matches_secure_policy_and_xfp_from_untrusted_peer_ignored(make_app, monkeypatch):
    monkeypatch.setenv("VIDEO_FACTORY_ENV", "production")
    monkeypatch.setenv("VIDEO_FACTORY_REQUIRE_ORIGIN", "0")
    app, _ = make_app()
    c = TestClient(app)
    sid = _sid(_signup(c, "lo@example.com"))
    r = c.post("/auth/logout", headers={**_ck(sid), "X-CSRF-Token": _csrf(sid)})
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    assert "Secure" in sc and ("Max-Age=0" in sc or "expires=" in sc.lower()), sc
    # dev mode: X-Forwarded-Proto from an UNTRUSTED peer must not flip the flag; from a trusted one it does
    monkeypatch.setenv("VIDEO_FACTORY_ENV", "dev")
    untrusted = TestClient(app, client=("198.51.100.9", 1))
    sc = _signup(untrusted, "xfp1@example.com", headers={"X-Forwarded-Proto": "https"}).headers.get("set-cookie", "")
    assert "Secure" not in sc
    trusted = TestClient(app, client=("127.0.0.1", 1))
    sc = _signup(trusted, "xfp2@example.com", headers={"X-Forwarded-Proto": "HTTPS, http"}).headers.get("set-cookie", "")
    assert "Secure" in sc
    sc = _signup(trusted, "xfp3@example.com", headers={"X-Forwarded-Proto": "http"}).headers.get("set-cookie", "")
    assert "Secure" not in sc


# ════════════════════════════════════════════════════════════════════
# 12. F9: require_csrf, odd stored expiry values
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("expires", ["", "garbage", "2001-01-01T00:00:00Z", "2001-01-01 00:00:00", "9999-99-99T99:99:99Z"])
def test_qa_f9_unparseable_or_past_expiry_is_never_treated_as_live(make_app, expires):
    from fastapi import Depends
    from auth.dependencies import require_csrf
    app, repo = make_app()

    @app.post("/_qa/csrf-probe", dependencies=[Depends(require_csrf)])
    async def _probe():
        return {"ok": True}
    sid, uid = _owner(app, f"f9-{secrets.token_hex(3)}@example.com")
    repo._conn().execute("UPDATE sessions SET expires_at=? WHERE session_id=?", (expires, sid))
    repo._conn().commit()
    r = TestClient(app).post("/_qa/csrf-probe", headers={**_ck(sid), "X-CSRF-Token": _csrf(sid)})
    assert r.status_code == 401, (expires, r.status_code, r.text)


def test_qa_f9_deleted_user_with_live_session_row_is_refused(make_app):
    from fastapi import Depends
    from auth.dependencies import require_csrf
    app, repo = make_app()

    @app.post("/_qa/csrf-probe2", dependencies=[Depends(require_csrf)])
    async def _probe():
        return {"ok": True}
    sid, uid = _owner(app, "ghost-user@example.com")
    repo._conn().execute("PRAGMA foreign_keys=OFF")
    repo._conn().execute("DELETE FROM users WHERE user_id=?", (uid,))
    repo._conn().commit()
    r = TestClient(app).post("/_qa/csrf-probe2", headers={**_ck(sid), "X-CSRF-Token": _csrf(sid)})
    assert r.status_code == 401, (r.status_code, r.text)
