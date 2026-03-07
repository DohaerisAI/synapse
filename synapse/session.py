from __future__ import annotations

from .models import RunState


TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.RECEIVED: {RunState.CONTEXT_BUILT, RunState.FAILED, RunState.CANCELLED},
    RunState.CONTEXT_BUILT: {RunState.PLANNED, RunState.FAILED, RunState.CANCELLED},
    RunState.PLANNED: {
        RunState.WAITING_INPUT,
        RunState.WAITING_APPROVAL,
        RunState.EXECUTING,
        RunState.RESPONDING,
        RunState.FAILED,
        RunState.CANCELLED,
    },
    RunState.WAITING_INPUT: {RunState.PLANNED, RunState.EXECUTING, RunState.CANCELLED, RunState.FAILED},
    RunState.WAITING_APPROVAL: {RunState.EXECUTING, RunState.FAILED, RunState.CANCELLED},
    RunState.EXECUTING: {RunState.VERIFYING, RunState.RESPONDING, RunState.FAILED, RunState.CANCELLED},
    RunState.VERIFYING: {RunState.WAITING_APPROVAL, RunState.RESPONDING, RunState.FAILED, RunState.CANCELLED},
    RunState.RESPONDING: {RunState.COMPLETED, RunState.FAILED},
    RunState.COMPLETED: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
}


class SessionStateMachine:
    def assert_transition(self, current: RunState, target: RunState) -> None:
        allowed = TRANSITIONS[current]
        if target not in allowed:
            raise ValueError(f"illegal transition {current} -> {target}")
