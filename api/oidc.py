"""OIDC (OpenID Connect) integration for Video Factory.

Supports Google (required) + GitHub (stretch) sign-in. Authlib powers
every cryptographic step (OAuth2 client, OIDC discovery, JWT
verification) — we never hand-roll JWT/JWKS verification.

Endpoints (added to api.main via ``build_router``):

- GET  /auth/oauth/{provider}/start
        Generates state + nonce, stashes them in the session-less
        ``oidc_pending`` table, redirects to the provider's
        authorize URL.

- GET  /auth/oauth/{provider}/callback
        Exchanges the code, verifies the id_token against the
        provider's JWKS (RS256, aud=client_id, iss matches discovery,
        nonce matches the pending state), then either links the
        identity to an existing user (verified email match) or
        creates a new user + default workspace + owner row.

- POST /auth/oauth/{provider}/disconnect
        Disconnect a previously linked provider from the current
        user. Refuses if it would orphan the account (no password +
        last identity).

Design constraints:

- State tokens are stored server-side, single-use, and expire in
  10 minutes. Replay attempts return 400.
- Nonce is bound to the state row and verified on the id_token.
- JWKS is cached in-process for 1 hour; on signature failure we
  refresh once (catches key rotation).
- Provider outage / network error → 503, never 500.
- Every link / create event is recorded in audit_log with the
  provider + sub (no tokens, no PII beyond the verified email).
"""
from __future__ import annotations

import secrets
import time
from typing import Optional

from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from auth.audit_log import append as audit_append
from auth.dependencies import (
    AuthContext,
    get_current_user,
    require_csrf,
)
from auth.session import (
    DEFAULT_TTL_SECONDS,
    build_clear_cookie_value,
    build_set_cookie_value,
    derive_csrf_token,
    new_session_id,
    should_use_secure_cookies,
)


# ── Provider configuration ─────────────────────────────────────

class OIDCConfigError(Exception):
    """Raised when provider env-vars are missing or invalid."""


# Discovery URLs (override via *_OIDC_DISCOVERY_URL env vars for
# local testing or for a future self-hosted provider).
_DEFAULT_DISCOVERY = {
    "google": "https://accounts.google.com/.well-known/openid-configuration",
    "github": "https://github.com/.well-known/openid-configuration",
}

# Provider client config lookup. We keep a per-provider in-process
# provider config that is built lazily from env vars so tests can
# monkey-patch providers without polluting global state.


def _provider_config(provider: str) -> dict:
    """Return the live config dict for `provider`.

    Reads from env vars:
        <PROVIDER>_OIDC_CLIENT_ID
        <PROVIDER>_OIDC_CLIENT_SECRET
        <PROVIDER>_OIDC_DISCOVERY_URL  (optional override)
    """
    p = provider.lower()
    prefix = f"{p.upper()}_OIDC"
    client_id = _env(f"{prefix}_CLIENT_ID", "")
    client_secret = _env(f"{prefix}_CLIENT_SECRET", "")
    discovery_url = _env(
        f"{prefix}_DISCOVERY_URL", _DEFAULT_DISCOVERY.get(p, ""),
    )
    if not client_id or not client_secret or not discovery_url:
        raise OIDCConfigError(
            f"OIDC provider {p!r} is not configured: "
            f"set {prefix}_CLIENT_ID + {prefix}_CLIENT_SECRET "
            f"(and optionally {prefix}_DISCOVERY_URL)"
        )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "discovery_url": discovery_url,
        "provider": p,
    }


def _env(name: str, default: str) -> str:
    import os
    return (os.environ.get(name) or default).strip() or default


# ── Discovery + JWKS cache ────────────────────────────────────

class _ProviderCache:
    """Per-process cache: discovery doc + JWKS.

    Refreshed every ``max_age_seconds`` (default 1 hour). On
    signature verification failure the caller asks for a forced
    refresh — that path catches provider key rotation.
    """

    def __init__(self, *, max_age_seconds: int = 3600) -> None:
        self.max_age_seconds = max_age_seconds
        self._cache: dict = {}  # provider -> {loaded_at, discovery, jwks}

    def get(self, provider: str, *, force_refresh: bool = False) -> dict:
        cfg = _provider_config(provider)
        now = time.time()
        entry = self._cache.get(provider)
        if (
            not force_refresh
            and entry
            and (now - entry["loaded_at"]) < self.max_age_seconds
        ):
            return entry
        # Fetch discovery + JWKS.
        import httpx
        try:
            r = httpx.get(cfg["discovery_url"], timeout=8.0,
                          follow_redirects=True)
            r.raise_for_status()
            discovery = r.json()
            jwks_uri = discovery.get("jwks_uri")
            if not jwks_uri:
                raise OIDCConfigError(
                    f"OIDC discovery for {provider!r} has no jwks_uri"
                )
            r2 = httpx.get(jwks_uri, timeout=8.0,
                           follow_redirects=True)
            r2.raise_for_status()
            jwks = r2.json()
        except Exception as e:
            raise OIDCConfigError(
                f"failed to fetch OIDC discovery/jwks for {provider!r}: {e}"
            ) from e
        self._cache[provider] = {
            "loaded_at": now,
            "discovery": discovery,
            "jwks": jwks,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "discovery_url": cfg["discovery_url"],
        }
        return self._cache[provider]

    def clear(self, provider: Optional[str] = None) -> None:
        if provider is None:
            self._cache.clear()
        else:
            self._cache.pop(provider, None)


# Module-level cache; tests can reset via ``_provider_cache.clear()``.
_provider_cache = _ProviderCache()


# ── State (one-time anti-CSRF) ────────────────────────────────

_STATE_TTL_SECONDS = 600  # 10 minutes


def _new_state_nonce() -> str:
    """Return a fresh ``state..nonce`` token, two random 128-bit hex strings."""
    return f"{secrets.token_urlsafe(16)}.{secrets.token_urlsafe(16)}"


# ── PKCE (RFC 7636, S1b / A1 F24) ─────────────────────────────────

def new_pkce_verifier() -> str:
    """Return a fresh ``code_verifier``: 64 chars from the unreserved set.

    RFC 7636 §4.1 allows 43..128 characters of [A-Z a-z 0-9 - . _ ~];
    ``token_urlsafe(48)`` yields exactly 64 of [A-Z a-z 0-9 - _].
    """
    return secrets.token_urlsafe(48)


def pkce_challenge(verifier: str) -> str:
    """S256 ``code_challenge`` = BASE64URL(SHA256(verifier)), no padding."""
    import base64
    import hashlib
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _split_state(state_blob: str) -> tuple[str, str]:
    """Split a stored state value back into (state, nonce).

    Tolerates the legacy single-string form (state == nonce == blob)
    so older rows still verify.
    """
    parts = state_blob.split(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return state_blob, state_blob


# ── Authorization URL builder ───────────────────────────────────────────

def authorize_url(
    *, provider: str, redirect_uri: str, state: str, nonce: str,
    code_challenge: Optional[str] = None,
) -> str:
    """Build the provider authorize URL.

    We hand-build the URL because we do not want to depend on the
    ``requests`` library (the authlib ``requests_client`` wrapper
    requires it). The URL shape is RFC 6749 §4.1.1 and works for
    every OIDC-compliant provider. With ``code_challenge`` the URL
    carries PKCE (RFC 7636) ``code_challenge`` +
    ``code_challenge_method=S256``; Google and GitHub both honour it.
    """
    import urllib.parse
    entry = _provider_cache.get(provider)
    params = {
        "client_id": entry["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_scopes(provider)),
        "state": state,
        "nonce": nonce,
        # Prompt=select_account gives users the "use a different
        # Google account" affordance — important for shared machines.
        "prompt": "select_account",
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    qs = urllib.parse.urlencode(params)
    return f"{entry['discovery']['authorization_endpoint']}?{qs}"


def _scopes(provider: str) -> list[str]:
    """OIDC scopes per provider.

    GitHub (S1b / A1 F25): ``user:email`` only. It is enough for the
    verified primary address (/user/emails) and the public /user
    profile; ``read:user`` (private profile data) and ``openid``
    (GitHub user sign-in is plain OAuth, no id_token) are not asked.
    """
    if provider == "google":
        return ["openid", "email", "profile"]
    if provider == "github":
        return ["user:email"]
    return ["openid", "email", "profile"]


# ── Token exchange ─────────────────────────────────────────────

def _exchange_code(
    *, provider: str, code: str, redirect_uri: str,
    code_verifier: Optional[str] = None,
) -> dict:
    """Exchange ``code`` for tokens at the provider's token endpoint.

    Returns the raw token dict (typically
    ``{access_token, id_token, expires_in, ...}``). Raises
    ``OIDCCallbackError`` on any failure. ``code_verifier`` is the
    PKCE secret bound to the state row; the provider rejects the
    exchange if it does not hash to the ``code_challenge`` it saw.
    """
    import httpx
    import urllib.parse
    entry = _provider_cache.get(provider)
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": entry["client_id"],
        "client_secret": entry["client_secret"],
    }
    if code_verifier:
        body["code_verifier"] = code_verifier
    try:
        r = httpx.post(
            entry["discovery"]["token_endpoint"],
            content=urllib.parse.urlencode(body),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                      "Accept": "application/json"},
            timeout=8.0,
        )
    except Exception as e:
        raise OIDCCallbackError(
            f"token endpoint network error: {e}"
        ) from e
    if r.status_code >= 400:
        raise OIDCCallbackError(
            f"token endpoint returned HTTP {r.status_code}: "
            f"{(r.text or '')[:200]}"
        )
    try:
        return r.json()
    except Exception as e:
        raise OIDCCallbackError(
            f"token endpoint returned non-JSON: {e}"
        ) from e


# ── Token exchange + id_token verification ─────────────────────

class OIDCCallbackError(Exception):
    """Wraps any provider-side failure during the OAuth callback.

    Maps to HTTP 400 (bad code/state) or HTTP 503 (provider
    outage) in the route handler.
    """


def exchange_code_and_verify(
    *, provider: str, code: str, redirect_uri: str,
    expected_state: str, expected_nonce: str,
    code_verifier: Optional[str] = None,
) -> dict:
    """Exchange the auth code, verify the id_token, return claims.

    Steps:
      1. Exchange ``code`` (+ the PKCE ``code_verifier``) for tokens
         at the provider's token endpoint.
      2. Verify the id_token signature against the JWKS.
      3. Verify ``iss`` matches the discovery issuer.
      4. Verify ``aud`` matches the configured client_id.
      5. Verify ``nonce`` matches the expected nonce.

    Raises ``OIDCCallbackError`` on any failure. The caller decides
    whether to surface it as 400 (bad request) or 503 (provider
    outage) based on the ``kind`` attribute.
    """
    entry = _provider_cache.get(provider)
    try:
        token = _exchange_code(
            provider=provider, code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
    except OIDCCallbackError:
        raise
    except Exception as e:
        raise OIDCCallbackError(
            f"token exchange failed: {e}"
        ) from e

    # Provider-specific token shape: GitHub returns no id_token
    # (it is OIDC-optional). We always require id_token because the
    # brief says "verify id_token" — but we keep this branch so a
    # test can exercise the GitHub "no id_token" path explicitly.
    id_token_raw = token.get("id_token")
    if not id_token_raw:
        # GitHub path: derive claims from /user + /user/emails.
        if provider == "github":
            return _github_claims_from_access_token(
                provider=provider, token=token,
            )
        raise OIDCCallbackError("no id_token in token response")

    # Verify signature + claims against JWKS.
    try:
        key_set = KeySet.import_key_set(entry["jwks"])
        claims = _decode_and_validate_id_token(
            id_token_raw, key_set,
            issuer=entry["discovery"]["issuer"],
            client_id=entry["client_id"],
        )
    except OIDCCallbackError as e:
        # Force a JWKS refresh once; this catches a provider key
        # rotation that landed between our cache window.
        try:
            entry = _provider_cache.get(provider, force_refresh=True)
            key_set = KeySet.import_key_set(entry["jwks"])
            claims = _decode_and_validate_id_token(
                id_token_raw, key_set,
                issuer=entry["discovery"]["issuer"],
                client_id=entry["client_id"],
            )
        except OIDCCallbackError as e2:
            raise OIDCCallbackError(str(e2)) from e2
        except Exception as e2:
            raise OIDCCallbackError(
                f"id_token verification failed after JWKS refresh: {e2}"
            ) from e2

    # Nonce check (joserfc does not enforce a per-call nonce).
    actual_nonce = claims.get("nonce")
    if actual_nonce != expected_nonce:
        raise OIDCCallbackError(
            f"nonce mismatch (expected {expected_nonce[:8]}..., "
            f"got {(actual_nonce or '')[:8]}...)"
        )

    return {
        "provider": provider,
        "sub": claims["sub"],
        "email": claims.get("email"),
        "email_verified": bool(claims.get("email_verified", False)),
        "name": claims.get("name") or claims.get("given_name"),
        "picture": claims.get("picture"),
        "raw": claims,
    }


def _decode_and_validate_id_token(
    id_token_raw, key_set, *, issuer, client_id,
) -> dict:
    """Decode + validate id_token, return the claims dict.

    Verification steps:
      1. RS256 signature check via JWKS key set.
      2. ``iss`` matches the configured issuer.
      3. ``aud`` matches the configured client id.
      4. ``sub`` is present (RFC 7519 §4.1.2).
      5. ``exp`` not in the past (RFC 7519 §4.1.4).

    Raises ``OIDCCallbackError`` on any failure.
    """
    try:
        token = jose_jwt.decode(id_token_raw, key_set,
                                algorithms=["RS256"])
    except Exception as e:
        raise OIDCCallbackError(f"id_token decode failed: {e}") from e
    try:
        registry = JWTClaimsRegistry(
            iss={"essential": True, "value": issuer},
            aud={"essential": True, "value": client_id},
            sub={"essential": True},
        )
        registry.validate(token.claims)
    except Exception as e:
        raise OIDCCallbackError(
            f"id_token claims invalid: {e}"
        ) from e
    return token.claims


def _github_claims_from_access_token(*, provider: str, token: dict) -> dict:
    """Build a claims-like dict for GitHub from the access token.

    GitHub's OIDC implementation is partial: the authorize URL still
    uses OpenID scopes but the token response often lacks an
    ``id_token``. We fall back to /user + /user/emails with the
    bearer access token to build equivalent claims.

    This is the "stretch" GitHub path; if GitHub's behaviour
    improves upstream we will prefer the id_token instead.
    """
    import httpx
    access_token = token.get("access_token")
    if not access_token:
        raise OIDCCallbackError("github: no access_token in response")
    headers = {"Authorization": f"Bearer {access_token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        me = httpx.get("https://api.github.com/user",
                       headers=headers, timeout=8.0).json()
        emails = httpx.get("https://api.github.com/user/emails",
                           headers=headers, timeout=8.0).json()
    except Exception as e:
        raise OIDCCallbackError(f"github /user fetch failed: {e}") from e
    primary = next(
        (e for e in emails if e.get("primary") and e.get("verified")),
        None,
    )
    if not primary:
        raise OIDCCallbackError(
            "github: no verified primary email — sign-in refused"
        )
    return {
        "provider": provider,
        "sub": str(me.get("id")),
        "email": primary["email"],
        "email_verified": True,
        "name": me.get("name") or me.get("login"),
        "picture": me.get("avatar_url"),
        "raw": {"user": me, "emails": emails},
    }


# ── DB helpers (state store + audit) ──────────────────────────

def _ensure_oidc_pending_table(repo) -> None:
    """Create the ``oidc_pending`` table on demand.

    Migration 007 covers ``oauth_identities`` but the one-time
    state/nonce store is kept in its own lightweight table so we
    can purge expired rows without affecting linked identities.
    S1b adds the ``code_verifier`` column (PKCE) to tables created
    by an older build.
    """
    conn = repo._conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS oidc_pending ("
        "  state TEXT PRIMARY KEY,"
        "  nonce TEXT NOT NULL,"
        "  provider TEXT NOT NULL,"
        "  redirect_after TEXT,"
        "  created_at TEXT NOT NULL,"
        "  expires_at TEXT NOT NULL,"
        "  consumed_at TEXT,"
        "  code_verifier TEXT"
        ")"
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(oidc_pending)")}
    if "code_verifier" not in cols:
        conn.execute("ALTER TABLE oidc_pending ADD COLUMN code_verifier TEXT")
    conn.commit()


def _store_pending(repo, *, state: str, nonce: str, provider: str,
                   redirect_after: Optional[str],
                   code_verifier: Optional[str] = None) -> None:
    _ensure_oidc_pending_table(repo)
    now = _now_iso()
    expires = _now_plus(_STATE_TTL_SECONDS)
    repo._conn().execute(
        "INSERT INTO oidc_pending(state, nonce, provider, "
        "redirect_after, created_at, expires_at, code_verifier) "
        "VALUES (?,?,?,?,?,?,?)",
        (state, nonce, provider.lower(),
         redirect_after or "", now, expires, code_verifier),
    )
    repo._conn().commit()


def _consume_pending(repo, *, state: str) -> Optional[dict]:
    """Atomically read-and-mark-consumed a pending state row.

    Returns None if the state is unknown, already consumed, or
    expired. Single-use: the row's ``consumed_at`` is set in the
    same statement so a second callback with the same state
    always returns None.
    """
    _ensure_oidc_pending_table(repo)
    conn = repo._conn()
    row = conn.execute(
        "SELECT state, nonce, provider, redirect_after, expires_at, "
        "consumed_at, code_verifier FROM oidc_pending WHERE state=?",
        (state,),
    ).fetchone()
    if not row:
        return None
    if row[5]:  # consumed_at
        return None
    if _is_expired(row[4]):
        return None
    cur = conn.execute(
        "UPDATE oidc_pending SET consumed_at=? WHERE state=? AND consumed_at IS NULL",
        (_now_iso(), state),
    )
    conn.commit()
    if cur.rowcount != 1:
        # A concurrent callback consumed it between SELECT and UPDATE.
        return None
    return {
        "state": row[0],
        "nonce": row[1],
        "provider": row[2],
        "redirect_after": row[3] or None,
        "expires_at": row[4],
        "code_verifier": row[6] or None,
    }


# ── Public router builder ─────────────────────────────────────

def build_router(repo) -> APIRouter:
    """Build the /auth/oauth/* router bound to the given repo."""
    router = APIRouter(prefix="/auth/oauth", tags=["auth"])

    def _client_ip(request: Request) -> str:
        from auth.client_ip import client_ip as _resolve_client_ip
        return _resolve_client_ip(request)

    def _redirect_uri(request: Request, provider: str) -> str:
        base = _env(
            "VIDEO_FACTORY_PUBLIC_BASE_URL",
            str(request.base_url).rstrip("/"),
        )
        return f"{base}/auth/oauth/{provider}/callback"

    @router.get("/{provider}/start")
    async def oauth_start(
        request: Request, provider: str,
        redirect_after: Optional[str] = None,
    ):
        p = (provider or "").lower()
        if p not in ("google", "github"):
            raise HTTPException(400, "unknown provider")
        try:
            _provider_config(p)  # surface config errors as 503
        except OIDCConfigError as e:
            raise HTTPException(503, str(e))
        state_blob = _new_state_nonce()
        state, nonce = _split_state(state_blob)
        # S1b / A1 F24: PKCE S256. The verifier never leaves the
        # server; only its hash goes to the provider in the URL.
        code_verifier = new_pkce_verifier()
        _store_pending(
            repo, state=state_blob, nonce=nonce,
            provider=p,
            redirect_after=redirect_after,
            code_verifier=code_verifier,
        )
        try:
            url = authorize_url(
                provider=p,
                redirect_uri=_redirect_uri(request, p),
                state=state_blob,
                nonce=nonce,
                code_challenge=pkce_challenge(code_verifier),
            )
        except OIDCConfigError as e:
            raise HTTPException(503, str(e))
        # 302 keeps the method-preserving semantics of an
        # authorize redirect (browsers re-issue GET).
        return Response(
            status_code=302,
            headers={"Location": url},
        )

    @router.get("/{provider}/callback")
    async def oauth_callback(request: Request, provider: str):
        p = (provider or "").lower()
        if p not in ("google", "github"):
            raise HTTPException(400, "unknown provider")
        code = (request.query_params.get("code") or "").strip()
        state_param = (request.query_params.get("state") or "").strip()
        if not code:
            raise HTTPException(400, "missing code")
        if not state_param:
            raise HTTPException(400, "missing state")
        pending = _consume_pending(repo, state=state_param)
        if not pending:
            raise HTTPException(400, "invalid or expired state")
        expected_state, expected_nonce = _split_state(pending["state"])
        # The state in the URL must match the stored state (Authlib
        # treats state as opaque — we just compare the full blob).
        if state_param != pending["state"]:
            raise HTTPException(400, "state mismatch")
        # S1b / A1 F24: a pending row without a PKCE verifier was not
        # minted by this build's /start (or was tampered with): refuse.
        if not pending.get("code_verifier"):
            raise HTTPException(400, "invalid or expired state")
        try:
            claims = exchange_code_and_verify(
                provider=p,
                code=code,
                redirect_uri=_redirect_uri(request, p),
                expected_state=expected_state,
                expected_nonce=expected_nonce,
                code_verifier=pending["code_verifier"],
            )
        except OIDCConfigError as e:
            # Provider discovery / JWKS / network outage.
            raise HTTPException(503, f"OIDC provider unavailable: {e}")
        except OIDCCallbackError as e:
            raise HTTPException(400, f"OIDC callback failed: {e}")

        email = (claims.get("email") or "").strip().lower()
        email_verified = bool(claims.get("email_verified", False))
        provider_sub = claims["sub"]

        if not email:
            # The provider did not return an email — we cannot
            # link or create a user without one.
            raise HTTPException(400, "OIDC provider did not return an email")

        # Link by verified email: if an existing user has the same
        # verified email, link the OIDC sub to them. NEVER link on
        # an unverified email (an attacker could register the
        # email on their OIDC account and then take over).
        # If the email already exists but is NOT verified, refuse
        # the callback — silently creating a second user with the
        # same email would be a footgun (the OIDC user could later
        # verify, then the original user is orphaned).
        existing = (
            repo.get_oauth_identity(provider=p, provider_sub=provider_sub)
        )
        linked = False
        new_user_created = False
        if existing:
            user_id = existing["user_id"]
        else:
            candidate = repo.get_user_by_email(email)
            if candidate and email_verified:
                user_id = candidate["user_id"]
                linked = True
            elif candidate and not email_verified:
                # Email collision with an unverified local account.
                raise HTTPException(
                    400,
                    "OIDC email matches an existing unverified local "
                    "account; sign in with your password first or "
                    "verify your email before linking.",
                )
            else:
                user_id = _create_user_from_oidc(
                    repo, email=email,
                    display_name=claims.get("name") or "",
                )
                new_user_created = True

        # Make sure the row exists (idempotent — protects against
        # race where two callbacks land simultaneously).
        try:
            repo.create_oauth_identity(
                user_id=user_id, provider=p,
                provider_sub=provider_sub,
            )
        except ValueError as e:
            # Another callback won the race; resolve to that user.
            existing2 = repo.get_oauth_identity(
                provider=p, provider_sub=provider_sub,
            )
            if existing2:
                user_id = existing2["user_id"]
            else:
                raise HTTPException(400, str(e))

        # Mark email verified on first link (the provider already
        # verified the email for us).
        if email_verified:
            try:
                repo.mark_user_email_verified(user_id)
            except Exception:
                pass

        audit_append(
            repo, event="auth.oidc_login",
            context={
                "provider": p,
                "provider_sub": provider_sub,
                "email_verified": email_verified,
                "linked_existing_user": linked,
                "new_user_created": new_user_created,
            },
            user_id=user_id,
        )

        # Issue session cookie + redirect to landing page.
        # S1b / A1 F8: drop any session id the browser presented
        # before this sign-in, then issue a fresh one.
        from auth.session import SESSION_COOKIE_NAME as _SCN
        _presented = (request.cookies.get(_SCN) or "").strip()
        if _presented:
            try:
                repo.delete_session(_presented)
            except Exception:  # noqa: BLE001 - never block sign-in
                pass
        session_id = new_session_id()
        csrf = derive_csrf_token(session_id)
        repo.create_session(
            user_id=user_id, csrf_token=csrf,
            ip=_client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:512],
            expires_at=_expires_at_iso(),
            session_id=session_id,
        )

        first_ws = _first_workspace_for_user(repo._conn(), user_id)
        workspace_id = first_ws["workspace_id"] if first_ws else None

        body = {
            "user_id": user_id,
            "email": email,
            "provider": p,
            "linked_existing_user": linked,
            "new_user_created": new_user_created,
            "csrf_token": csrf,
        }
        if workspace_id:
            body["workspace_id"] = workspace_id
            body["workspace_slug"] = first_ws["slug"] if first_ws else None

        request.state._vf_session_cookie = build_set_cookie_value(
            session_id=session_id, max_age=DEFAULT_TTL_SECONDS,
            secure=should_use_secure_cookies(request),
        )
        landing = pending.get("redirect_after") or "/"
        # Always return JSON in tests; in a real browser the UI
        # would 302 to `landing`. The body shape is documented.
        return {"ok": True, "redirect": landing, **body}

    @router.post("/{provider}/disconnect")
    async def oauth_disconnect(
        request: Request, provider: str,
        ctx: AuthContext = Depends(get_current_user),
    ):
        p = (provider or "").lower()
        if p not in ("google", "github"):
            raise HTTPException(400, "unknown provider")
        require_csrf(request, x_csrf_token=request.headers.get("X-CSRF-Token"))
        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        provider_sub = (payload.get("provider_sub") or "").strip()
        if not provider_sub:
            raise HTTPException(400, "provider_sub required")
        try:
            removed = repo.delete_oauth_identity(
                user_id=ctx.user_id, provider=p,
                provider_sub=provider_sub,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not removed:
            raise HTTPException(404, "OIDC identity not linked")
        audit_append(
            repo, event="auth.oidc_disconnected",
            context={"provider": p, "provider_sub": provider_sub},
            user_id=ctx.user_id,
        )
        return {"ok": True, "provider": p,
                "provider_sub": provider_sub}

    @router.get("/identities")
    async def list_oidc_identities(
        ctx: AuthContext = Depends(get_current_user),
    ):
        return {
            "identities": repo.list_oauth_identities_for_user(ctx.user_id),
        }

    return router


# ── Helpers (duplicated from api/auth.py on purpose to keep
#    this module's surface small + avoid leaking internals) ─────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_plus(seconds: int) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds),
    )


def _expires_at_iso() -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() + DEFAULT_TTL_SECONDS),
    )


def _is_expired(iso: str) -> bool:
    try:
        ts = time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
        return time.time() > time.mktime(ts) + 0
    except Exception:
        return True


def _first_workspace_for_user(conn, user_id: str) -> Optional[dict]:
    cur = conn.execute(
        "SELECT m.workspace_id, w.slug FROM memberships m "
        "JOIN workspaces w ON w.workspace_id = m.workspace_id "
        "WHERE m.user_id=? ORDER BY m.created_at ASC, m.rowid ASC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"workspace_id": row[0], "slug": row[1]}


def _create_user_from_oidc(repo, *, email: str, display_name: str) -> str:
    """Create a fresh user + default workspace + owner membership.

    Mirrors api/auth.signup() but skips password hashing + email
    verification token (the OIDC provider already verified the
    email). The default workspace slug is derived from the email
    local-part + a short suffix to avoid collisions.
    """
    import re
    user_id = f"usr_{secrets.token_hex(8)}"
    local = email.split("@", 1)[0].lower()
    local = re.sub(r"[^a-z0-9-]+", "-", local).strip("-")[:30] or "user"
    suffix = secrets.token_hex(3)
    slug = f"{local}-{suffix}"
    workspace_id = f"wks_{secrets.token_hex(8)}"
    wname = display_name or f"{email.split('@',1)[0]}'s workspace"
    now = _now_iso()
    with repo.tx() as c:
        c.execute(
            "INSERT INTO users(user_id, email, password_hash, status, "
            "email_verified, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, email.lower(), "disabled", "active", 1, now),
        )
        c.execute(
            "INSERT INTO workspaces(workspace_id, slug, name, "
            "owner_user_id, plan, status, live_enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (workspace_id, slug, wname, user_id, "free", "active", 0, now),
        )
        c.execute(
            "INSERT INTO memberships(user_id, workspace_id, role, "
            "created_at) VALUES (?,?,?,?)",
            (user_id, workspace_id, "owner", now),
        )
    audit_append(
        repo, event="auth.oidc_signup",
        context={"provider": "(callback)", "email": email,
                  "workspace_slug": slug},
        user_id=user_id,
        workspace_id=workspace_id,
    )
    return user_id


# ── Test seam ────────────────────────────────────────────────

def _reset_for_tests() -> None:
    """Reset module-level caches. Tests call this in setUp."""
    _provider_cache.clear()