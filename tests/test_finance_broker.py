"""Tests for finance broker policy — RED phase."""
from __future__ import annotations

import pytest

from synapse.broker import CapabilityBroker
from synapse.models import PlannedAction


@pytest.fixture
def broker():
    return CapabilityBroker()


# All read/analysis actions should be safe (no approval)
@pytest.mark.parametrize("action", [
    "finance.holdings.read",
    "finance.positions.read",
    "finance.margins.read",
    "finance.mf.holdings",
    "finance.mf.nav_history",
    "finance.mf.sip_xirr",
    "finance.technical.analyze",
    "finance.technical.scan",
    "finance.chart.capture",
    "finance.chart.analyze",
    "finance.sentiment.analyze",
    "finance.macro.summary",
    "finance.portfolio.summary",
    "finance.portfolio.risk",
    "finance.trade.suggest",
])
def test_finance_read_actions_are_safe(broker, action):
    decision = broker.decide(PlannedAction(action=action, payload={}))
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_trade_gtt_place_requires_approval(broker):
    decision = broker.decide(PlannedAction(action="finance.trade.gtt_place", payload={}))
    assert decision.allowed is True
    assert decision.requires_approval is True


def test_unknown_finance_action_defaults_safe(broker):
    """Any finance.* action not explicitly trade.gtt_place should be safe."""
    decision = broker.decide(PlannedAction(action="finance.future.action", payload={}))
    assert decision.allowed is True
    assert decision.requires_approval is False
