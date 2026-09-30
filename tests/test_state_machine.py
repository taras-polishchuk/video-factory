"""State machine tests."""

from __future__ import annotations

import pytest

from control_plane.errors import InvalidStateTransition
from control_plane.state_machine import STATE_GRAPH, State, VideoStateMachine


def test_state_graph_is_explicit():
    # State graph must list every adjacency explicitly.
    expected = {
        State.DRAFT, State.RESEARCHING, State.PREPARING, State.NEEDS_REVIEW,
        State.READY_FOR_RENDER, State.QUEUED, State.SUBMITTING, State.PROCESSING,
        State.OUTPUTS_READY, State.QC, State.ASSEMBLING, State.DONE,
        State.FAILED, State.CANCELLED,
    }
    assert set(STATE_GRAPH.keys()) == expected


def test_terminal_states():
    sm = VideoStateMachine(initial=State.DONE)
    assert sm.is_terminal()
    sm2 = VideoStateMachine(initial=State.CANCELLED)
    assert sm2.is_terminal()


def test_valid_path():
    sm = VideoStateMachine()
    sm.transition(State.RESEARCHING, actor="t")
    sm.transition(State.PREPARING, actor="t")
    sm.transition(State.READY_FOR_RENDER, actor="t")
    sm.transition(State.QUEUED, actor="t")
    sm.transition(State.SUBMITTING, actor="t")
    sm.transition(State.PROCESSING, actor="t")
    sm.transition(State.OUTPUTS_READY, actor="t")
    sm.transition(State.QC, actor="t")
    sm.transition(State.ASSEMBLING, actor="t")
    sm.transition(State.DONE, actor="t")
    assert sm.is_terminal()


def test_invalid_transition_raises():
    sm = VideoStateMachine()
    with pytest.raises(InvalidStateTransition):
        sm.transition(State.QUEUED, actor="t")  # can't skip DRAFT directly to QUEUED


def test_audit_recorded_on_every_transition():
    sm = VideoStateMachine(campaign_id="cmp_x", video_id="vid_y")
    sm.transition(State.RESEARCHING, actor="t", reason="start")
    sm.transition(State.PREPARING, actor="t")
    recs = sm.audit.records()
    assert len(recs) == 2
    assert recs[0].event == "state.transition"
    assert recs[0].context["to"] == "RESEARCHING"


def test_done_state_has_no_outgoing_transitions():
    sm = VideoStateMachine(initial=State.DONE)
    assert sm.can(State.QUEUED) is False
    assert sm.can(State.FAILED) is False


def test_failed_state_can_recover_to_draft():
    sm = VideoStateMachine(initial=State.FAILED)
    sm.transition(State.DRAFT, actor="recovery")
    assert sm.state == State.DRAFT