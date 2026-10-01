"""Wires the existing BudgetGuard into a real preflight + spend path.

Used by the JobRunner before each render-stage submit. Real orchestrator
code in control_plane/orchestrator.py still constructs a BudgetGuard but
never invokes preflight; this module provides the missing piece.
"""
from __future__ import annotations

from control_plane.budget import BudgetDecision, BudgetGuard


class BudgetEnforcer:
    """Tracks per-company budget with hard preflight + record_spend."""

    def __init__(self, *, max_cost_per_video_usd: float,
                 max_cost_per_batch_usd: float) -> None:
        self.guard = BudgetGuard(
            max_cost_per_video_usd=max_cost_per_video_usd,
            max_cost_per_batch_usd=max_cost_per_batch_usd,
        )

    def preflight(self, *, estimated_cost_usd, scope: str = "video") -> BudgetDecision:
        return self.guard.preflight(estimated_cost_usd=estimated_cost_usd, scope=scope)

    def record_spend(self, amount_usd: float) -> None:
        self.guard.record_spend(amount_usd)

    @property
    def spent(self) -> float:
        return self.guard.spent


def preflight_or_block(enforcer: BudgetEnforcer, *, estimated_cost_usd) -> BudgetDecision:
    """Convenience: raise BudgetExceeded if not allowed."""
    from control_plane.errors import BudgetExceeded
    decision = enforcer.preflight(estimated_cost_usd=estimated_cost_usd)
    if not decision.allowed:
        raise BudgetExceeded(decision.reason)
    return decision