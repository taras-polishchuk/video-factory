"""Atomic job admission: quota + budget preflight + INSERT (BLK-1).

Before this module the API handler did ``count_jobs_since`` (SELECT),
then ``create_job`` (INSERT), and the budget preflight ran later in the
background thread, after the API had already answered 200. Two
concurrent submits could both pass "1 job left" and both start; a live
job with an unknown cost was accepted and only failed later.

Now ``JobRunner.create_job_admitted`` calls ``Repo.create_job`` with an
``admission`` callback that runs inside the job INSERT's
``BEGIN IMMEDIATE`` transaction:

1. re-count this workspace's jobs this month on the transaction's
   connection and apply ``core.quotas.check_job_quota``;
2. reserve the job's estimated cost on the runner's ``BudgetGuard``
   (atomic preflight + spend; unknown cost is refused);
3. only then is the job row inserted.

Any refusal raises ``JobAdmissionRefused`` before the INSERT, so the
API never returns 2xx for a refused job. The API maps the exception to
its HTTP status (429 quota, 402 budget) and writes the audit row.
"""
from __future__ import annotations

from typing import Optional

from core.audit import EVENT_BUDGET_REFUSED, EVENT_QUOTA_EXCEEDED


class JobAdmissionRefused(Exception):
    """A job submit was refused by atomic admission.

    ``status_code`` + ``body`` are the HTTP response; ``audit_event`` +
    ``audit_context`` describe the audit row to append (the API layer
    owns the actor attribution).
    """

    def __init__(self, *, status_code: int, body: dict,
                 audit_event: str, audit_context: dict,
                 workspace_id: Optional[str] = None,
                 user_id: Optional[str] = None,
                 actor: Optional[str] = None) -> None:
        super().__init__(body.get("error", "job admission refused"))
        self.status_code = int(status_code)
        self.body = dict(body)
        self.audit_event = audit_event
        self.audit_context = dict(audit_context)
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.actor = actor


def quota_refusal(decision, *, workspace_id: str, endpoint: str,
                  user_id: Optional[str] = None,
                  actor: Optional[str] = None) -> JobAdmissionRefused:
    """Same 429 body + ``quota.exceeded`` context as the handler's
    pre-check, so a client cannot tell which of the two refused it."""
    return JobAdmissionRefused(
        status_code=429,
        body={
            "error": (
                f"FREE plan limit {decision.limit} jobs/month "
                f"reached; resets at {decision.reset_at}"
            ),
            "quota_reset_at": decision.reset_at,
            "jobs_used": decision.jobs_used,
            "jobs_remaining": 0,
        },
        audit_event=EVENT_QUOTA_EXCEEDED,
        audit_context={
            "workspace_id": workspace_id,
            "plan": decision.plan,
            "jobs_used": decision.jobs_used,
            "quota_reset_at": decision.reset_at,
            "endpoint": endpoint,
            "admission": "atomic",
        },
        workspace_id=workspace_id, user_id=user_id, actor=actor,
    )


def budget_refusal(decision, *, workspace_id: str, endpoint: str,
                   render_profile: str, unknown: bool,
                   user_id: Optional[str] = None,
                   actor: Optional[str] = None) -> JobAdmissionRefused:
    """402 Payment Required: the budget guard refused the estimate.

    ``reason_code`` is ``unknown_cost`` (live adapter without a known
    price: never auto-approved) or ``ceiling`` (per-video or batch
    ceiling would be exceeded).
    """
    reason_code = "unknown_cost" if unknown else "ceiling"
    return JobAdmissionRefused(
        status_code=402,
        body={
            "error": f"budget preflight refused: {decision.reason}",
            "reason_code": reason_code,
            "render_profile": render_profile,
            "estimated_cost_usd": decision.estimated_cost_usd,
            "ceiling_usd": decision.ceiling_usd,
        },
        audit_event=EVENT_BUDGET_REFUSED,
        audit_context={
            "workspace_id": workspace_id,
            "render_profile": render_profile,
            "reason_code": reason_code,
            "reason": decision.reason,
            "estimated_cost_usd": decision.estimated_cost_usd,
            "ceiling_usd": decision.ceiling_usd,
            "endpoint": endpoint,
        },
        workspace_id=workspace_id, user_id=user_id, actor=actor,
    )
