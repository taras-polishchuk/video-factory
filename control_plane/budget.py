"""Budget guard. Preflight cost estimation; refuses submit on exceed.

Hard preflight + record_spend. Used by Orchestrator + JobRunner before
ANY submit to a live provider. The guard refuses on:

  * unknown cost (estimate returns CostEstimate(unknown=True))
  * per-video ceiling exceeded
  * batch ceiling exceeded (cumulative across multiple submits)

For the live path to be reachable at all, both `LIVE_PROVIDER_TESTS=true`
AND `SMOKE_TEST_PASSED=true` must be set. Either flag missing keeps the
adapter in lock-down. See `render_plane/gate.py` for the unified gate.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Optional

from control_plane.errors import BudgetExceeded


# Both flags must be set for a live provider adapter to permit submit().
# SMOKE_TEST_PASSED is a one-way flip: the operator flips it after a
# dedicated smoke test on a separate account confirmed the contract works.
LIVE_OK_ENV = "SMOKE_TEST_PASSED"
LIVE_OK_LIVE_FLAG = "LIVE_PROVIDER_TESTS"


def live_path_unlocked() -> bool:
    """True iff BOTH `LIVE_PROVIDER_TESTS=true` AND `SMOKE_TEST_PASSED=true`.

    Read at submit() time. Cache module config (L-12). Either flag missing
    → live path stays locked. Default false.
    """
    live = os.environ.get(LIVE_OK_LIVE_FLAG, "").lower() == "true"
    smoke = os.environ.get(LIVE_OK_ENV, "").lower() == "true"
    return live and smoke


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    estimated_cost_usd: Optional[float]
    ceiling_usd: Optional[float]
    reason: str = ""


class BudgetGuard:
    """In-memory cumulative budget. Estimates come from renderer.estimate().

    Thread-safe: ``reserve`` / ``release`` / ``record_spend`` serialise
    on an internal lock, so concurrent submits cannot both fit into the
    last slice of the batch ceiling (BLK-1).
    """

    def __init__(
        self,
        *,
        max_cost_per_video_usd: float,
        max_cost_per_batch_usd: float,
    ) -> None:
        self._max_video = float(max_cost_per_video_usd)
        self._max_batch = float(max_cost_per_batch_usd)
        self._batch_spent = 0.0
        self._lock = threading.Lock()

    def preflight(
        self,
        *,
        estimated_cost_usd: Optional[float],
        scope: str = "video",
    ) -> BudgetDecision:
        if estimated_cost_usd is None:
            return BudgetDecision(
                allowed=False,
                estimated_cost_usd=None,
                ceiling_usd=None,
                reason="unknown cost; refusing without LIVE_PROVIDER_TESTS budget verification",
            )
        if scope == "video" and estimated_cost_usd > self._max_video:
            return BudgetDecision(
                allowed=False,
                estimated_cost_usd=estimated_cost_usd,
                ceiling_usd=self._max_video,
                reason=f"per-video ceiling {self._max_video}",
            )
        if self._batch_spent + estimated_cost_usd > self._max_batch:
            return BudgetDecision(
                allowed=False,
                estimated_cost_usd=estimated_cost_usd,
                ceiling_usd=self._max_batch,
                reason=f"batch ceiling {self._max_batch}; spent {self._batch_spent}",
            )
        return BudgetDecision(
            allowed=True,
            estimated_cost_usd=estimated_cost_usd,
            ceiling_usd=self._max_batch,
            reason="ok",
        )

    def record_spend(self, amount_usd: float) -> None:
        if amount_usd < 0:
            raise BudgetExceeded("negative spend")
        with self._lock:
            self._batch_spent += amount_usd

    def reserve(
        self,
        *,
        estimated_cost_usd: Optional[float],
        scope: str = "video",
    ) -> BudgetDecision:
        """Atomic preflight + record_spend.

        Returns the preflight decision; when it is allowed the estimate
        is already counted as spent. Undo with ``release`` if the work
        is abandoned before the provider is charged.
        """
        with self._lock:
            decision = self.preflight(
                estimated_cost_usd=estimated_cost_usd, scope=scope,
            )
            if decision.allowed:
                self._batch_spent += float(estimated_cost_usd or 0.0)
            return decision

    def release(self, amount_usd: Optional[float]) -> None:
        """Give back a reservation that was never charged."""
        if not amount_usd:
            return
        with self._lock:
            self._batch_spent = max(0.0, self._batch_spent - float(amount_usd))

    @property
    def spent(self) -> float:
        return self._batch_spent


def preflight_or_block(guard: BudgetGuard, *, estimated_cost_usd) -> BudgetDecision:
    """Raise ``BudgetExceeded`` when the guard refuses the estimate.

    MAJ-2: moved here from the deleted ``core/budget_enforcer.py`` shim
    (its ``BudgetEnforcer`` was a 1:1 delegate of ``BudgetGuard``).
    """
    decision = guard.preflight(estimated_cost_usd=estimated_cost_usd)
    if not decision.allowed:
        raise BudgetExceeded(decision.reason)
    return decision