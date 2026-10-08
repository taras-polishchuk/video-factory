"""Plan-based quota checks (Wave 6b).

Pure functions that take ``(workspace_id, plan, now)`` plus a
``job_count_for_month`` callable, and return a typed decision
``QuotaDecision(allowed, remaining, reset_at, limit, plan)``.

Wave 6b is FREE-tier only:

* FREE: 5 jobs per calendar month, reset on the first day of the
  next month at 00:00 UTC.
* PRO / ENTERPRISE: unlimited jobs (and unlimited workspaces).
* `reset_at` is the ISO-8601 UTC timestamp of the next reset
  (None for paid plans since they never reset).

Why pure functions and no in-memory cache:

* The brief says the check must re-read plan synchronously on
  every call.  Plan is the source of truth (the DB column on
  `workspaces`); a Stripe webhook (Wave 6c) will flip plan via a
  plain ``UPDATE workspaces SET plan=...``.  An in-memory cache
  would race the webhook and let a FREE user keep submitting after
  upgrade.  Better to do 1 extra SELECT per submit than ship a
  quota bypass.

* The check function itself does not touch the DB; it takes the
  current job count as a parameter.  This keeps it trivially
  unit-testable without fixtures.

Usage from the API layer::

    from core.quotas import check_job_quota, FREE_JOB_LIMIT

    decision = check_job_quota(
        workspace_id=wid,
        plan=ws["plan"],
        now=datetime.now(timezone.utc),
        job_count_for_month=lambda wid, since_iso: repo.count_jobs_since(
            workspace_id=wid, since_iso=since_iso,
        ),
    )
    if not decision.allowed:
        raise HTTPException(
            429, detail={"quota_reset_at": decision.reset_at,
                         "jobs_used": decision.jobs_used,
                         "jobs_remaining": 0})

For workspace-count limits see ``count_user_workspaces_plan_limit``
and ``WORKSPACE_LIMIT_FREE``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional


# ── Limits (single source of truth) ────────────────────────────

# FREE plan: 5 jobs per calendar month, 1 workspace per user.
FREE_JOB_LIMIT: int = 5
WORKSPACE_LIMIT_FREE: int = 1

# Paid plans: unlimited.
UNLIMITED: int = -1


# ── Decision shape ─────────────────────────────────────────────


@dataclass(frozen=True)
class QuotaDecision:
    """Result of a quota check.

    * ``allowed`` — True if the action may proceed.
    * ``limit``   — max jobs/period for this plan.  ``-1`` means
      "unlimited" (paid plans).
    * ``jobs_used`` / ``jobs_remaining`` — current period usage.
      ``remaining`` is ``UNLIMITED`` for paid plans (use
      ``allowed`` to gate, not the count).
    * ``reset_at`` — ISO-8601 UTC timestamp when the counter
      resets (None for paid plans).
    * ``plan``     — the plan the decision was made for.
    * ``reason``   — human-readable string for audit rows; never
      shown to API callers (we use the dedicated 429 / 403 bodies).
    """

    allowed: bool
    plan: str
    limit: int
    jobs_used: int
    jobs_remaining: int
    reset_at: Optional[str]
    reason: str

    def to_dict(self) -> dict:
        return {
            "plan": self.plan,
            "limit": self.limit,
            "jobs_used": self.jobs_used,
            "jobs_remaining": self.jobs_remaining,
            "reset_at": self.reset_at,
            "allowed": self.allowed,
        }


# ── Helpers ────────────────────────────────────────────────────


def _month_start(now: datetime) -> datetime:
    """Return the UTC midnight at the start of ``now``'s month."""
    return now.astimezone(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )


def _next_month_start(now: datetime) -> datetime:
    """Return the UTC midnight at the start of the month AFTER
    ``now``'s month.  Handles December → January of the next year.
    """
    ms = _month_start(now)
    if ms.month == 12:
        return ms.replace(year=ms.year + 1, month=1)
    return ms.replace(month=ms.month + 1)


def _iso(dt: datetime) -> str:
    """ISO-8601 in UTC with a trailing Z (matches ``now_iso`` in
    core/repo.py: ``%Y-%m-%dT%H:%M:%SZ``).
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Job quota check (FREE = 5/month) ───────────────────────────


def check_job_quota(
    *,
    workspace_id: str,
    plan: str,
    now: datetime,
    job_count_for_month: Callable[[str, str], int],
) -> QuotaDecision:
    """Decide whether ``workspace_id`` may submit another job.

    ``now`` MUST be a timezone-aware datetime in UTC (the brief is
    explicit about never using naive datetimes).  ``job_count_for_month``
    is injected so the function stays pure / unit-testable; the
    caller is responsible for the actual SELECT.  The signature is
    ``job_count_for_month(workspace_id, since_iso) -> int`` where
    ``since_iso`` is the first instant of the current month in the
    same ISO format used by ``now_iso`` in core/repo.py.

    Paid plans (pro / enterprise) short-circuit to
    ``allowed=True, limit=UNLIMITED, remaining=UNLIMITED,
    reset_at=None`` WITHOUT calling the injected count function
    (no DB round-trip for paid workspaces).
    """
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime, got %r" % type(now))
    if now.tzinfo is None:
        raise ValueError(
            "now must be timezone-aware (UTC); got a naive datetime"
        )

    plan = (plan or "free").lower()

    if plan in ("pro", "enterprise"):
        return QuotaDecision(
            allowed=True,
            plan=plan,
            limit=UNLIMITED,
            jobs_used=0,
            jobs_remaining=UNLIMITED,
            reset_at=None,
            reason="paid plan: unlimited jobs",
        )

    # FREE / unknown plan
    month_start = _month_start(now)
    next_reset = _next_month_start(now)
    used = int(
        job_count_for_month(workspace_id, _iso(month_start)) or 0
    )
    remaining = max(0, FREE_JOB_LIMIT - used)
    if remaining <= 0:
        return QuotaDecision(
            allowed=False,
            plan="free",
            limit=FREE_JOB_LIMIT,
            jobs_used=used,
            jobs_remaining=0,
            reset_at=_iso(next_reset),
            reason=(
                f"FREE plan limit {FREE_JOB_LIMIT} jobs/month reached; "
                f"resets {_iso(next_reset)}"
            ),
        )
    return QuotaDecision(
        allowed=True,
        plan="free",
        limit=FREE_JOB_LIMIT,
        jobs_used=used,
        jobs_remaining=remaining,
        reset_at=_iso(next_reset),
        reason=(
            f"FREE plan: {remaining}/{FREE_JOB_LIMIT} jobs remaining "
            f"this month (resets {_iso(next_reset)})"
        ),
    )


# ── Workspace count (FREE = 1 per user) ───────────────────────


@dataclass(frozen=True)
class WorkspaceLimitDecision:
    """Result of a per-user workspace-count check (FREE = 1)."""

    allowed: bool
    plan: str
    limit: int
    workspaces_owned: int
    reason: str


def check_workspace_count(
    *,
    plan: str,
    workspaces_owned: int,
) -> WorkspaceLimitDecision:
    """Decide whether a user may create another workspace.

    Pro / Enterprise: unlimited (any positive count is allowed).
    FREE: at most ``WORKSPACE_LIMIT_FREE`` (1) workspace per user.

    The function is pure: it takes the *current* workspace count
    (read by the caller from `memberships JOIN workspaces WHERE
    role='owner' AND user_id=?`) and returns the decision.  We do
    NOT query the DB here.
    """
    plan = (plan or "free").lower()
    if plan in ("pro", "enterprise"):
        return WorkspaceLimitDecision(
            allowed=True, plan=plan, limit=UNLIMITED,
            workspaces_owned=int(workspaces_owned),
            reason="paid plan: unlimited workspaces",
        )
    owned = int(workspaces_owned or 0)
    if owned >= WORKSPACE_LIMIT_FREE:
        return WorkspaceLimitDecision(
            allowed=False, plan="free", limit=WORKSPACE_LIMIT_FREE,
            workspaces_owned=owned,
            reason=(
                f"FREE plan limit {WORKSPACE_LIMIT_FREE} workspace per "
                f"user reached (you have {owned})"
            ),
        )
    return WorkspaceLimitDecision(
        allowed=True, plan="free", limit=WORKSPACE_LIMIT_FREE,
        workspaces_owned=owned,
        reason=(
            f"FREE plan: {owned}/{WORKSPACE_LIMIT_FREE} workspaces used"
        ),
    )


# ── Live-provider gate (FREE = forbidden, even with env flag) ──


@dataclass(frozen=True)
class LiveGateDecision:
    """Result of the live-provider gate check.

    The check is the conjunction of FOUR independent conditions:

    1. ``live_set`` — ``LIVE_PROVIDER_TESTS=true`` in env
       (operator-level opt-in, default off).
    2. ``smoke_set`` — ``SMOKE_TEST_PASSED=true`` in env
       (smoke-account gate, default off).
    3. ``ws_live_enabled`` — workspace.live_enabled == True
       (per-workspace flag, default False).
    4. ``plan_allows_live`` — plan != 'free' (paid plans only).

    All four must be true for the live path to be allowed.  This
    module owns the *plan + workspace* half of the gate; the env
    half (``LIVE_PROVIDER_TESTS && SMOKE_TEST_PASSED``) is owned
    by ``render_plane.gate.ensure_live_unlocked`` and is checked
    separately at the provider boundary.
    """

    allowed: bool
    plan_allows_live: bool
    ws_live_enabled: bool
    reason: str


# Live provider profile names (everything in the registry except
# "mock").  Kept here (not imported from render_plane.registry)
# to avoid an import cycle: render_plane imports from control_plane
# which is fine, but core/* importing render_plane/* breaks the
# "core is leaf" rule the rest of the codebase follows.  If a new
# live adapter is added to render_plane/registry.py, add its profile
# here too — a small list beat a circular import any day.
LIVE_RENDER_PROFILES: frozenset[str] = frozenset({
    "heygen_direct",
    "comfy_cloud",
    "comfy_gpu_worker",
    "comfy_heygen_partner",
})


def is_live_profile(profile: Optional[str]) -> bool:
    """True if ``profile`` is a non-mock provider that should be
    gated by ``check_live_provider_allowed``.

    Unknown profiles (e.g. a typo, a future profile) are treated
    as LIVE: the gate refuses them rather than silently letting
    the user through.  This is the conservative choice — a typo
    hits the live gate and gets a 403 with a clear reason, instead
    of hitting a downstream UnknownProfile error in the registry.
    """
    if not profile:
        return False  # no profile specified → default path is mock
    return str(profile).lower() != "mock"


def check_live_provider_allowed(
    *,
    plan: str,
    workspace_live_enabled: bool,
    render_profile: Optional[str] = None,
) -> LiveGateDecision:
    """Decide whether live providers are allowed for this workspace.

    This is the *plan + workspace* half of the live gate; the env
    half (``LIVE_PROVIDER_TESTS && SMOKE_TEST_PASSED``) is owned
    by ``render_plane.gate.ensure_live_unlocked`` and is checked
    separately at the provider boundary.  Call this from the
    API layer BEFORE submitting a job whose ``render_profile``
    points at a live adapter, so a FREE user gets a 403 at submit
    time rather than a 500 mid-run.

    ``render_profile`` is OPTIONAL.  When provided, mock jobs
    short-circuit to ``allowed=True`` regardless of plan (mock
    providers are not "live" — they never call out to a paid
    vendor).  When the profile is a live adapter, the function
    applies the plan + workspace flag gate.  When the profile is
    unknown / None, the function defaults to the plan-based gate
    so a typo in a future profile doesn't sneak through.

    Paid plans + workspace.live_enabled=True + live profile:
    allowed=True.
    Anything else with a live profile: allowed=False.
    Mock profile: always allowed.
    """
    if render_profile is not None and not is_live_profile(
        render_profile
    ):
        # Mock (or empty) — gate passes unconditionally.  We
        # still emit a decision so callers can log it uniformly.
        return LiveGateDecision(
            allowed=True,
            plan_allows_live=(plan or "free").lower() in (
                "pro", "enterprise",
            ),
            ws_live_enabled=bool(workspace_live_enabled),
            reason=(
                f"render_profile={render_profile!r} is mock, "
                "live gate does not apply"
            ),
        )
    plan = (plan or "free").lower()
    plan_allows_live = plan in ("pro", "enterprise")
    ws_live_enabled = bool(workspace_live_enabled)
    if not plan_allows_live:
        return LiveGateDecision(
            allowed=False,
            plan_allows_live=False,
            ws_live_enabled=ws_live_enabled,
            reason=(
                "live providers are not available on the FREE plan; "
                "upgrade to Pro or Enterprise"
            ),
        )
    if not ws_live_enabled:
        return LiveGateDecision(
            allowed=False,
            plan_allows_live=True,
            ws_live_enabled=False,
            reason=(
                "live providers require workspace.live_enabled=True; "
                "operator has not enabled this workspace"
            ),
        )
    return LiveGateDecision(
        allowed=True,
        plan_allows_live=True,
        ws_live_enabled=True,
        reason="live providers allowed (plan + workspace flag set)",
    )
