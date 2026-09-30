"""Budget guard. Preflight cost estimation; refuses submit on exceed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from control_plane.errors import BudgetExceeded


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    estimated_cost_usd: Optional[float]
    ceiling_usd: Optional[float]
    reason: str = ""


class BudgetGuard:
    """In-memory cumulative budget. Estimates come from renderer.estimate()."""

    def __init__(
        self,
        *,
        max_cost_per_video_usd: float,
        max_cost_per_batch_usd: float,
    ) -> None:
        self._max_video = float(max_cost_per_video_usd)
        self._max_batch = float(max_cost_per_batch_usd)
        self._batch_spent = 0.0

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
        self._batch_spent += amount_usd

    @property
    def spent(self) -> float:
        return self._batch_spent