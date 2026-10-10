"""Login attempt tracking + rate limit decisions.

Tracks per-IP and per-email failed attempts in the `login_attempts`
table. The decision function is pure: given a count of recent
attempts and the configured policy, return "allow" / "lockout".

Policy (from the brief):

- 5 failed attempts per IP per 15 minutes → lockout
- 10 failed attempts per email per hour → lockout
- A successful attempt does NOT clear the counter; instead, the
  counter is per-window so a long pause naturally resets it.

Lockout is per-(ip,email) pair only in the audit row; the per-IP
window is the binding lockout signal at the API.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


# Defaults can be overridden via env so tests can shrink the window.
WINDOW_IP_SEC = int(__import__("os").environ.get("VF_LOGIN_IP_WINDOW_SEC", str(15 * 60)))
WINDOW_EMAIL_SEC = int(__import__("os").environ.get("VF_LOGIN_EMAIL_WINDOW_SEC", str(60 * 60)))
LIMIT_IP = int(__import__("os").environ.get("VF_LOGIN_IP_LIMIT", "5"))
LIMIT_EMAIL = int(__import__("os").environ.get("VF_LOGIN_EMAIL_LIMIT", "10"))


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    reason: str  # "ok" / "ip_lockout" / "email_lockout"
    failed_in_window_ip: int
    failed_in_window_email: int


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cutoff_iso(window_seconds: int) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - window_seconds),
    )


def count_failed_in_window(repo, *, ip: Optional[str], email: Optional[str],
                            window_seconds: int) -> tuple[int, int]:
    """Return (ip_failures, email_failures) in the window.

    Both are independent queries; the caller ORs them via
    `decide()`. The repo here is `core.repo.Repo`.
    """
    cut = cutoff_iso(window_seconds)
    ip_fails = 0
    email_fails = 0
    if ip:
        row = repo._conn().execute(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE ip=? AND succeeded=0 AND attempted_at >= ?",
            (ip, cut),
        ).fetchone()
        ip_fails = int(row[0]) if row else 0
    if email:
        row = repo._conn().execute(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE email=? AND succeeded=0 AND attempted_at >= ?",
            (email, cut),
        ).fetchone()
        email_fails = int(row[0]) if row else 0
    return ip_fails, email_fails


def record_attempt(repo, *, ip: str, email: str, succeeded: bool) -> None:
    """Append a row to `login_attempts`."""
    import uuid as _u
    repo._conn().execute(
        "INSERT INTO login_attempts(attempt_id, ip, email, succeeded, attempted_at) "
        "VALUES (?,?,?,?,?)",
        (f"la_{_u.uuid4().hex[:12]}", ip, email, 1 if succeeded else 0, now_iso()),
    )
    repo._conn().commit()


def decide(repo, *, ip: Optional[str], email: Optional[str]) -> RateLimitDecision:
    """Return whether a login attempt should be allowed."""
    ip_fails, email_fails = count_failed_in_window(
        repo, ip=ip, email=email, window_seconds=WINDOW_IP_SEC,
    )
    # For email we use the larger window
    _, email_fails_window = count_failed_in_window(
        repo, ip=None, email=email, window_seconds=WINDOW_EMAIL_SEC,
    )
    if ip_fails >= LIMIT_IP:
        return RateLimitDecision(False, "ip_lockout", ip_fails, email_fails_window)
    if email_fails_window >= LIMIT_EMAIL:
        return RateLimitDecision(False, "email_lockout", ip_fails, email_fails_window)
    return RateLimitDecision(True, "ok", ip_fails, email_fails_window)


# ── Generic per-key counters (S1b / A1 F26) ──────────────────────
#
# /auth/signup, /auth/forgot, /auth/forgot/consume and /auth/verify
# count EVERY request (not only failures) in a sliding window, keyed
# by client IP (and by e-mail for /auth/forgot). Counters live in the
# lazily created ``rate_limit_hits`` table (same pattern as
# ``oidc_pending``; no migration number needed). Limits and windows
# are read from env at call time, so ops can tune them without a
# code change and tests can shrink them with monkeypatch.setenv.
#
#   bucket            key     default limit / window   env vars
#   signup_ip         IP      10 / 3600 s               VF_SIGNUP_IP_LIMIT, VF_SIGNUP_IP_WINDOW_SEC
#   forgot_ip         IP       5 /  900 s               VF_FORGOT_IP_LIMIT, VF_FORGOT_IP_WINDOW_SEC
#   forgot_email      email    3 / 3600 s               VF_FORGOT_EMAIL_LIMIT, VF_FORGOT_EMAIL_WINDOW_SEC
#   reset_consume_ip  IP      10 /  900 s               VF_RESET_CONSUME_IP_LIMIT, ..._WINDOW_SEC
#   verify_ip         IP      10 /  900 s               VF_VERIFY_IP_LIMIT, VF_VERIFY_IP_WINDOW_SEC
#   keys_reauth       user     5 /  900 s               VF_KEYS_REAUTH_LIMIT, ..._WINDOW_SEC
#
# (LIMIT=0 disables a bucket.)

# bucket -> (limit env, window env, default limit, default window s)
POLICIES: dict[str, tuple[str, str, int, int]] = {
    "signup_ip": ("VF_SIGNUP_IP_LIMIT", "VF_SIGNUP_IP_WINDOW_SEC", 10, 3600),
    "forgot_ip": ("VF_FORGOT_IP_LIMIT", "VF_FORGOT_IP_WINDOW_SEC", 5, 900),
    "forgot_email": ("VF_FORGOT_EMAIL_LIMIT", "VF_FORGOT_EMAIL_WINDOW_SEC", 3, 3600),
    "reset_consume_ip": ("VF_RESET_CONSUME_IP_LIMIT",
                         "VF_RESET_CONSUME_IP_WINDOW_SEC", 10, 900),
    "verify_ip": ("VF_VERIFY_IP_LIMIT", "VF_VERIFY_IP_WINDOW_SEC", 10, 900),
    # Re-auth on POST /api/v1/admin/keys/rotate, keyed by user id.
    "keys_reauth": ("VF_KEYS_REAUTH_LIMIT", "VF_KEYS_REAUTH_WINDOW_SEC", 5, 900),
}

# Rows older than this are pruned on every hit, whatever the bucket.
MAX_WINDOW_SEC = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class BucketDecision:
    allowed: bool
    bucket: str
    count: int
    limit: int
    window_seconds: int


def _env_int(name: str, default: int) -> int:
    import os
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def policy(bucket: str) -> tuple[int, int]:
    """Return ``(limit, window_seconds)`` for ``bucket``."""
    limit_env, window_env, limit, window = POLICIES[bucket]
    window = max(1, min(_env_int(window_env, window), MAX_WINDOW_SEC))
    return _env_int(limit_env, limit), window


def email_key(email: str) -> str:
    """Counter key for an e-mail: a digest, so the table holds no address."""
    import hashlib
    return hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()[:32]


def _ensure_hits_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rate_limit_hits ("
        "  bucket TEXT NOT NULL,"
        "  key TEXT NOT NULL,"
        "  hit_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_bucket_key "
        "ON rate_limit_hits(bucket, key, hit_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_at "
        "ON rate_limit_hits(hit_at)"
    )


def hit(repo, *, bucket: str, key: str) -> BucketDecision:
    """Count one request for ``(bucket, key)``; deny once over the limit.

    A denied request is NOT recorded, so a client that backs off is
    let back in when its oldest counted request leaves the window.
    """
    limit, window = policy(bucket)
    if limit <= 0:
        return BucketDecision(True, bucket, 0, limit, window)
    cut = cutoff_iso(window)
    with repo.tx() as c:
        _ensure_hits_table(c)
        c.execute(
            "DELETE FROM rate_limit_hits WHERE hit_at < ?",
            (cutoff_iso(MAX_WINDOW_SEC),),
        )
        row = c.execute(
            "SELECT COUNT(*) FROM rate_limit_hits "
            "WHERE bucket=? AND key=? AND hit_at >= ?",
            (bucket, key, cut),
        ).fetchone()
        n = int(row[0]) if row else 0
        if n >= limit:
            return BucketDecision(False, bucket, n, limit, window)
        c.execute(
            "INSERT INTO rate_limit_hits(bucket, key, hit_at) VALUES (?,?,?)",
            (bucket, key, now_iso()),
        )
    return BucketDecision(True, bucket, n + 1, limit, window)
