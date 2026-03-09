"""Tests for FinanceExecutor — RED phase for Phase 4."""
from __future__ import annotations

from typing import Any

import pytest

from synapse.mcp.types import MCPToolDefinition, MCPToolResult


class FakeMCPAdapter:
    """Fake adapter returning canned MCP responses for finance tests."""

    def __init__(self, server_id: str, responses: dict[str, Any] | None = None):
        self.server_id = server_id
        self.url = f"https://fake-{server_id}.com/mcp"
        self._connected = True
        self._responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def list_tools(self) -> list[MCPToolDefinition]:
        return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        self.calls.append((tool_name, arguments))
        data = self._responses.get(tool_name, {"status": "ok"})
        return MCPToolResult(tool_name=tool_name, success=True, data=data, latency_ms=10.0)

    async def health_check(self) -> bool:
        return self._connected


@pytest.fixture
def kite_adapter():
    return FakeMCPAdapter("kite", responses={
        "get_holdings": [
            {"symbol": "RELIANCE", "quantity": 10, "average_price": 2400.0, "last_price": 2500.0, "pnl": 1000.0},
        ],
        "get_positions": [
            {"symbol": "TCS", "quantity": 5, "buy_price": 3500.0, "last_price": 3600.0, "product": "CNC"},
        ],
        "get_margins": {"equity": {"available": 50000.0, "used": 10000.0}},
        "get_mf_holdings": [
            {"scheme_name": "Axis Bluechip", "scheme_code": "120503", "units": 100.0, "nav": 45.0},
        ],
        "place_gtt": {"order_id": "GTT_123", "status": "placed"},
    })


@pytest.fixture
def mfapi_adapter():
    return FakeMCPAdapter("mfapi", responses={
        "get_nav_history": {"scheme_code": "120503", "data": [{"date": "2026-01-01", "nav": 44.0}]},
    })


@pytest.fixture
def tradingview_adapter():
    return FakeMCPAdapter("tradingview", responses={
        "get_analysis": {"symbol": "RELIANCE", "recommendation": "BUY", "rsi": 55.0},
        "get_screener": [{"symbol": "INFY", "setup": "breakout"}],
        "get_sentiment": {"symbol": "TCS", "sentiment": "bullish"},
        "get_calendar": {"events": [{"name": "RBI Policy", "date": "2026-03-15"}]},
    })


@pytest.fixture
def registry(kite_adapter, mfapi_adapter, tradingview_adapter):
    from synapse.mcp.registry import MCPRegistry
    reg = MCPRegistry()
    # Direct inject without async register
    reg._adapters = {
        "kite": kite_adapter,
        "mfapi": mfapi_adapter,
        "tradingview": tradingview_adapter,
    }
    return reg


@pytest.fixture
def executor(registry):
    from synapse.finance.executor import FinanceExecutor
    return FinanceExecutor(mcp_registry=registry)


@pytest.mark.asyncio
async def test_holdings_read(executor, kite_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.holdings.read", payload={}))
    assert result.success is True
    assert ("get_holdings", {}) in kite_adapter.calls


@pytest.mark.asyncio
async def test_positions_read(executor, kite_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.positions.read", payload={}))
    assert result.success is True
    assert ("get_positions", {}) in kite_adapter.calls


@pytest.mark.asyncio
async def test_margins_read(executor, kite_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.margins.read", payload={}))
    assert result.success is True


@pytest.mark.asyncio
async def test_mf_holdings(executor, kite_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.mf.holdings", payload={}))
    assert result.success is True


@pytest.mark.asyncio
async def test_mf_nav_history(executor, mfapi_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.mf.nav_history", payload={"scheme_code": "120503"}))
    assert result.success is True
    assert ("get_nav_history", {"scheme_code": "120503"}) in mfapi_adapter.calls


@pytest.mark.asyncio
async def test_technical_analyze(executor, tradingview_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.technical.analyze", payload={"symbol": "RELIANCE"}))
    assert result.success is True
    assert tradingview_adapter.calls[-1][0] == "get_analysis"


@pytest.mark.asyncio
async def test_technical_scan(executor, tradingview_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.technical.scan", payload={}))
    assert result.success is True


@pytest.mark.asyncio
async def test_sentiment_analyze(executor, tradingview_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.sentiment.analyze", payload={"symbol": "TCS"}))
    assert result.success is True


@pytest.mark.asyncio
async def test_macro_summary(executor, tradingview_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.macro.summary", payload={}))
    assert result.success is True


@pytest.mark.asyncio
async def test_portfolio_summary(executor, kite_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.portfolio.summary", payload={}))
    assert result.success is True


@pytest.mark.asyncio
async def test_trade_suggest(executor, tradingview_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.trade.suggest", payload={"symbol": "RELIANCE"}))
    assert result.success is True


@pytest.mark.asyncio
async def test_trade_gtt_place(executor, kite_adapter):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(
        action="finance.trade.gtt_place",
        payload={"symbol": "RELIANCE", "trigger_price": 2450.0, "quantity": 10, "transaction_type": "BUY", "order_type": "LIMIT"},
    ))
    assert result.success is True
    assert ("place_gtt", {"symbol": "RELIANCE", "trigger_price": 2450.0, "quantity": 10, "transaction_type": "BUY", "order_type": "LIMIT"}) in kite_adapter.calls


@pytest.mark.asyncio
async def test_mcp_not_connected(registry):
    from synapse.finance.executor import FinanceExecutor
    from synapse.models import PlannedAction
    # Remove kite adapter
    registry._adapters.pop("kite", None)
    executor = FinanceExecutor(mcp_registry=registry)
    result = await executor.execute(PlannedAction(action="finance.holdings.read", payload={}))
    assert result.success is False
    assert "not connected" in result.detail.lower() or "not configured" in result.detail.lower()


@pytest.mark.asyncio
async def test_unsupported_finance_action(executor):
    from synapse.models import PlannedAction
    result = await executor.execute(PlannedAction(action="finance.unknown.action", payload={}))
    assert result.success is False
    assert "unsupported" in result.detail.lower()
