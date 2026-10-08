"""Single source of truth for Wave 6b audit event names.

The DB-backed `audit_log` table is written by exactly ONE helper
throughout the codebase: ``auth.audit_log.append`` (best-effort,
never raises) and ``Repo.append_audit`` (the equivalent method
on the repo).  The brief for Wave 6b is explicit: do NOT bypass
the existing audit path, just call it with the canonical event
names below.

Why this module exists (instead of inlining strings at the call
site):

* Three new audit events land in Wave 6b (``quota.exceeded``,
  ``quota.workspace_limit_hit``, ``live_provider.blocked``).
  Hard-coding the strings at every emit site makes typos silent
  (the DB has no FK on the ``event`` column, so a typo just
  shows up as a row nobody queries).  Centralizing them here
  makes the grep-rename proof.

* Future waves (e.g. 6c — webhook → plan changes) will add more
  events.  Keeping them in one file means a code reviewer can
  verify the full event taxonomy in a 30-line file rather than
  grepping the API.

The event names are intentionally short, dotted, and past-tense
where possible (``quota.exceeded`` not ``quota_exceeded_event``)
to match the existing convention used by the billing endpoints
(``billing.checkout_started``, ``billing.checkout_failed``,
``billing.portal_opened``, ``billing.portal_failed``).
"""
from __future__ import annotations

from typing import Optional

# ── Wave 6b event names (do not edit without a search-rename) ──

EVENT_QUOTA_EXCEEDED: str = "quota.exceeded"
"""Emitted when a FREE workspace tries to submit a 6th job in the
same calendar month (UTC).  Context MUST include
``workspace_id``, ``plan``, ``jobs_used``, ``quota_reset_at``.
"""

EVENT_QUOTA_WORKSPACE_LIMIT_HIT: str = "quota.workspace_limit_hit"
"""Emitted when a FREE user tries to create a 2nd workspace via
POST /api/v1/auth/workspaces or POST /api/v1/companies (legacy
alias).  Context MUST include ``user_id``, ``plan``,
``workspaces_owned``.
"""

EVENT_LIVE_PROVIDER_BLOCKED: str = "live_provider.blocked"
"""Emitted at submit_job time when the live-provider gate refuses
a request (plan=free, or workspace.live_enabled=false, or both).
Context MUST include ``workspace_id``, ``plan``,
``ws_live_enabled``, ``render_profile``, ``reason``.
"""


# ── Thin emit helper ──────────────────────────────────────────


def emit(
    repo,
    *,
    event: str,
    context: Optional[dict] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    actor: Optional[str] = None,
) -> None:
    """Append a row to the DB-backed ``audit_log`` table.

    Thin wrapper around ``Repo.append_audit`` so the call sites
    only have to import one name.  Never raises — a missed audit
    row is better than a missed business action (this matches
    the policy in ``auth/audit_log.append`` and
    ``Repo.append_audit``).
    """
    try:
        repo.append_audit(
            event=event,
            context=context or {},
            user_id=user_id,
            workspace_id=workspace_id,
            actor=actor,
        )
    except Exception:
        # Defensive: Repo.append_audit is documented as
        # never-raises, but a future regression there must not
        # take down a billing / quota gate.
        pass
