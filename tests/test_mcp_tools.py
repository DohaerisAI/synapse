"""Tests for MCP tools — RED → GREEN."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse.tools.registry import ToolRegistry, ToolResult


def _mcp_tool(name: str, description: str = "", input_schema: dict | None = None):
    """Create a fake MCP tool definition (SimpleNamespace avoids MagicMock .name conflict)."""
    return SimpleNamespace(name=name, description=description, input_schema=input_schema or {})


class TestMCPToolRegistration:
    @pytest.mark.asyncio
    async def test_register_mcp_server_tools(self):
        from synapse.tools.mcp_tools import register_mcp_server_tools

        reg = ToolRegistry()
        adapter = MagicMock()
        adapter.list_tools = AsyncMock(
            return_value=[
                _mcp_tool("get_holdings", "Get holdings", {"type": "object"}),
                _mcp_tool("place_order", "Place order", {"type": "object"}),
                _mcp_tool("get_profile", "Get profile"),
            ]
        )
        count = await register_mcp_server_tools(reg, "kite", adapter)
        assert count == 3
        assert reg.get("kite.get_holdings") is not None
        assert reg.get("kite.place_order") is not None
        assert reg.get("kite.get_profile") is not None

    @pytest.mark.asyncio
    async def test_tool_names_prefixed(self):
        from synapse.tools.mcp_tools import register_mcp_server_tools

        reg = ToolRegistry()
        adapter = MagicMock()
        adapter.list_tools = AsyncMock(
            return_value=[_mcp_tool("analyze", "Analyze")]
        )
        await register_mcp_server_tools(reg, "tradingview", adapter)
        t = reg.get("tradingview.analyze")
        assert t is not None
        assert t.category == "mcp.tradingview"

    @pytest.mark.asyncio
    async def test_trade_tools_need_approval(self):
        from synapse.tools.mcp_tools import register_mcp_server_tools

        reg = ToolRegistry()
        adapter = MagicMock()
        adapter.list_tools = AsyncMock(
            return_value=[
                _mcp_tool("place_order", "Place order"),
                _mcp_tool("place_gtt", "Place GTT"),
                _mcp_tool("get_holdings", "Get holdings"),
            ]
        )
        await register_mcp_server_tools(reg, "kite", adapter)
        assert reg.get("kite.place_order").check_approval({}) is True
        assert reg.get("kite.place_gtt").check_approval({}) is True
        assert reg.get("kite.get_holdings").check_approval({}) is False

    @pytest.mark.asyncio
    async def test_mcp_tool_execution(self):
        from synapse.tools.mcp_tools import register_mcp_server_tools
        from synapse.mcp.types import MCPToolResult

        reg = ToolRegistry()
        adapter = MagicMock()
        adapter.list_tools = AsyncMock(
            return_value=[_mcp_tool("get_holdings", "Get holdings")]
        )
        adapter.call_tool = AsyncMock(
            return_value=MCPToolResult(tool_name="get_holdings", success=True, data={"qty": 100})
        )
        await register_mcp_server_tools(reg, "kite", adapter)
        tool = reg.get("kite.get_holdings")
        result = await tool.execute({"symbol": "RELIANCE"}, ctx=MagicMock())
        assert isinstance(result, ToolResult)
        assert result.error is None
        adapter.call_tool.assert_awaited_once_with("get_holdings", {"symbol": "RELIANCE"})

    @pytest.mark.asyncio
    async def test_mcp_tool_failure(self):
        from synapse.tools.mcp_tools import register_mcp_server_tools
        from synapse.mcp.types import MCPToolResult

        reg = ToolRegistry()
        adapter = MagicMock()
        adapter.list_tools = AsyncMock(
            return_value=[_mcp_tool("get_data", "Get data")]
        )
        adapter.call_tool = AsyncMock(
            return_value=MCPToolResult(tool_name="get_data", success=False, error="timeout")
        )
        await register_mcp_server_tools(reg, "srv", adapter)
        tool = reg.get("srv.get_data")
        result = await tool.execute({}, ctx=MagicMock())
        assert result.error is not None
        assert "timeout" in result.error
