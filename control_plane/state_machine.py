"""State machine. Explicit states and explicit transition graph.

States (minimum, per prompt):
  DRAFT, RESEARCHING, PREPARING, NEEDS_REVIEW, READY_FOR_RENDER,
  QUEUED, SUBMITTING, PROCESSING, OUTPUTS_READY, QC, ASSEMBLING,
  DONE, FAILED, CANCELLED

Every transition is recorded in the audit log. Invalid transitions
raise InvalidStateTransition.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Optional

from control_plane.errors import InvalidStateTransition
from control_plane.audit import AuditLog, InMemoryAuditLog, AuditRecord, now_iso


class State(str, Enum):
    DRAFT = "DRAFT"
    RESEARCHING = "RESEARCHING"
    PREPARING = "PREPARING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY_FOR_RENDER = "READY_FOR_RENDER"
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    PROCESSING = "PROCESSING"
    OUTPUTS_READY = "OUTPUTS_READY"
    QC = "QC"
    ASSEMBLING = "ASSEMBLING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Adjacency: only edges listed here are valid. No silent relaxation.
STATE_GRAPH: Dict[State, FrozenSet[State]] = {
    State.DRAFT: frozenset({State.RESEARCHING, State.CANCELLED}),
    State.RESEARCHING: frozenset({State.PREPARING, State.FAILED, State.CANCELLED}),
    State.PREPARING: frozenset({State.NEEDS_REVIEW, State.READY_FOR_RENDER, State.FAILED, State.CANCELLED}),
    State.NEEDS_REVIEW: frozenset({State.READY_FOR_RENDER, State.PREPARING, State.CANCELLED}),
    State.READY_FOR_RENDER: frozenset({State.QUEUED, State.CANCELLED}),
    State.QUEUED: frozenset({State.SUBMITTING, State.CANCELLED}),
    State.SUBMITTING: frozenset({State.PROCESSING, State.FAILED, State.QUEUED, State.CANCELLED}),
    State.PROCESSING: frozenset({State.OUTPUTS_READY, State.FAILED, State.CANCELLED}),
    State.OUTPUTS_READY: frozenset({State.QC, State.FAILED}),
    State.QC: frozenset({State.ASSEMBLING, State.FAILED}),
    State.ASSEMBLING: frozenset({State.DONE, State.FAILED}),
    State.DONE: frozenset(),  # terminal
    State.FAILED: frozenset({State.DRAFT}),  # recovery via explicit reset
    State.CANCELLED: frozenset(),  # terminal
}


TERMINAL_STATES: FrozenSet[State] = frozenset({State.DONE, State.CANCELLED})


class Transition:
    """Single state transition event. Emitted by the state machine."""

    def __init__(
        self,
        *,
        from_state: State,
        to_state: State,
        actor: str,
        campaign_id: Optional[str] = None,
        video_id: Optional[str] = None,
        scene_id: Optional[str] = None,
        reason: str = "",
        context: Optional[dict] = None,
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.actor = actor
        self.campaign_id = campaign_id
        self.video_id = video_id
        self.scene_id = scene_id
        self.reason = reason
        self.context = context or {}
        self.at = now_iso()


class VideoStateMachine:
    """One machine per video (or scene). Audit-log-integrated."""

    def __init__(
        self,
        *,
        initial: State = State.DRAFT,
        campaign_id: Optional[str] = None,
        video_id: Optional[str] = None,
        scene_id: Optional[str] = None,
        audit: Optional[AuditLog] = None,
    ) -> None:
        self.state: State = initial
        self.campaign_id = campaign_id
        self.video_id = video_id
        self.scene_id = scene_id
        self._audit: InMemoryAuditLog = audit if isinstance(audit, InMemoryAuditLog) else InMemoryAuditLog()
        if audit is not None and not isinstance(audit, InMemoryAuditLog):
            # Mirror records into a concrete log so tests can inspect them.
            # (V1: only InMemoryAuditLog is supported for direct records().)
            for r in getattr(audit, "records", lambda: [])():
                self._audit.record(r)
        self.history: list[Transition] = []

    def can(self, to_state: State) -> bool:
        return to_state in STATE_GRAPH[self.state]

    def transition(
        self,
        to_state: State,
        *,
        actor: str,
        reason: str = "",
        context: Optional[dict] = None,
    ) -> Transition:
        if not self.can(to_state):
            raise InvalidStateTransition(
                f"cannot transition from {self.state.value} to {to_state.value}"
            )
        tr = Transition(
            from_state=self.state,
            to_state=to_state,
            actor=actor,
            campaign_id=self.campaign_id,
            video_id=self.video_id,
            scene_id=self.scene_id,
            reason=reason,
            context=context,
        )
        self.history.append(tr)
        prev = self.state
        self.state = to_state
        self._audit.record(
            AuditRecord(
                timestamp=tr.at,
                event="state.transition",
                actor=actor,
                campaign_id=self.campaign_id,
                video_id=self.video_id,
                scene_id=self.scene_id,
                context={
                    "from": prev.value,
                    "to": to_state.value,
                    "reason": reason,
                    **(context or {}),
                },
            )
        )
        return tr

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def audit(self) -> AuditLog:
        return self._audit