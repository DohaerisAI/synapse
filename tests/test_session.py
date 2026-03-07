import pytest

from synapse.models import RunState
from synapse.session import SessionStateMachine


def test_state_machine_accepts_documented_transition() -> None:
    SessionStateMachine().assert_transition(RunState.RECEIVED, RunState.CONTEXT_BUILT)


def test_state_machine_rejects_invalid_transition() -> None:
    with pytest.raises(ValueError):
        SessionStateMachine().assert_transition(RunState.RECEIVED, RunState.COMPLETED)
