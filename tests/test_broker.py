from synapse.broker import CapabilityBroker
from synapse.models import PlannedAction


def test_broker_requires_approval_for_global_memory_writes() -> None:
    decision = CapabilityBroker().decide(
        PlannedAction(action="memory.write", payload={"scope": "global", "content": "note"})
    )
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.executor == "host"


def test_broker_blocks_patch_application() -> None:
    decision = CapabilityBroker().decide(PlannedAction(action="code.patch.apply"))
    assert decision.allowed is False
    assert decision.executor == "none"


def test_broker_allows_gws_reads_without_approval() -> None:
    decision = CapabilityBroker().decide(PlannedAction(action="gws.gmail.search", payload={"query": "invoice"}))
    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.executor == "host"


def test_broker_requires_approval_for_gws_send() -> None:
    decision = CapabilityBroker().decide(PlannedAction(action="gws.gmail.send", payload={"to": "a@b.com", "subject": "x", "body": "y"}))
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.executor == "host"


def test_broker_allows_gws_inspect_without_approval() -> None:
    decision = CapabilityBroker().decide(PlannedAction(action="gws.inspect", payload={"argv": ["gmail", "--help"], "service": "gmail"}))
    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.executor == "host"
