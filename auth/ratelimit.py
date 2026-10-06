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
