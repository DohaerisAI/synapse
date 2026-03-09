"""Tests for finance capability registrations — RED phase."""
from __future__ import annotations

from synapse.capabilities import DEFAULT_CAPABILITY_REGISTRY


EXPECTED_FINANCE_ACTIONS = [
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
    "finance.trade.gtt_place",
]


def test_all_finance_capabilities_registered():
    for action in EXPECTED_FINANCE_ACTIONS:
        cap = DEFAULT_CAPABILITY_REGISTRY.get(action)
        assert cap is not None, f"missing capability: {action}"
        assert cap.family == "finance"


def test_finance_family_bundle():
    bundle = DEFAULT_CAPABILITY_REGISTRY.prompt_bundle(family="finance")
    for action in EXPECTED_FINANCE_ACTIONS:
        assert action in bundle


def test_finance_capability_count():
    bundle = DEFAULT_CAPABILITY_REGISTRY.prompt_bundle(family="finance")
    lines = [line for line in bundle.strip().splitlines() if line.strip()]
    assert len(lines) == len(EXPECTED_FINANCE_ACTIONS)
