from __future__ import annotations

from .models import RunState


# Simplified state machine for ReAct loop pipeline.
# PLANNED, WAITING_INPUT, WAITING_APPROVAL, VERIFYING kept in enum for DB compat
# but only 5 active states are used: RECEIVED → CONTEXT_BUILT → EXECUTING → RESPONDING → COMPLETED
TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.RECEIVED: {RunState.CONTEXT_BUILT, RunState.FAILED, RunState.CANCELLED},
    RunState.CONTEXT_BUILT: {RunState.PLANNED, RunState.EXECUTING, RunState.RESPONDING, RunState.FAILED, RunState.CANCELLED},
    RunState.PLANNED: {RunState.WAITING_INPUT, RunState.WAITING_APPROVAL, RunState.EXECUTING, RunState.RESPONDING, RunState.FAILED, RunState.CANCELLED},
    RunState.WAITING_INPUT: {RunState.PLANNED, RunState.EXECUTING, RunState.CANCELLED, RunState.FAILED},
    RunState.WAITING_APPROVAL: {RunState.EXECUTING, RunState.RESPONDING, RunState.FAILED, RunState.CANCELLED},
    RunState.EXECUTING: {RunState.WAITING_APPROVAL, RunState.VERIFYING, RunState.RESPONDING, RunState.FAILED, RunState.CANCELLED},
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
