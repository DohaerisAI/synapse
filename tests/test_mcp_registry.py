"""Tests for MCPRegistry — RED phase."""
from __future__ import annotations

import pytest

from synapse.mcp.types import MCPAuth, MCPConnectionStatus, MCPToolDefinition


class FakeAdapter:
    """Minimal fake adapter for registry tests."""

    def __init__(self, server_id: str, tools: list[MCPToolDefinition] | None = None):
        self.server_id = server_id
        self._tools = tools or []
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def list_tools(self) -> list[MCPToolDefinition]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict) -> object:
        from synapse.mcp.types import MCPToolResult
        return MCPToolResult(tool_name=tool_name, success=True, data={"mock": True})

    async def health_check(self) -> bool:
        return self._connected


@pytest.mark.asyncio
async def test_register_and_list():
    from synapse.mcp.registry import MCPRegistry
    registry = MCPRegistry()
    tools = [
        MCPToolDefinition(name="get_holdings", description="Holdings", input_schema={}, server_id="kite"),
    ]
    fake = FakeAdapter("kite", tools)
    await registry.register_adapter(fake)
    connected = registry.list_connected()
    assert len(connected) == 1
    assert connected[0].server_id == "kite"
    assert connected[0].status == MCPConnectionStatus.CONNECTED


@pytest.mark.asyncio
async def test_unregister():
    from synapse.mcp.registry import MCPRegistry
    registry = MCPRegistry()
    fake = FakeAdapter("kite")
    await registry.register_adapter(fake)
    await registry.unregister("kite")
    assert registry.get("kite") is None
    assert fake.connected is False


@pytest.mark.asyncio
async def test_discover_all_tools():
    from synapse.mcp.registry import MCPRegistry
    registry = MCPRegistry()
    tools1 = [MCPToolDefinition(name="get_holdings", description="H", input_schema={}, server_id="kite")]
    tools2 = [MCPToolDefinition(name="get_nav", description="N", input_schema={}, server_id="mfapi")]
    await registry.register_adapter(FakeAdapter("kite", tools1))
    await registry.register_adapter(FakeAdapter("mfapi", tools2))
    all_tools = await registry.discover_all_tools()
    assert len(all_tools) == 2
    names = {t.name for t in all_tools}
    assert names == {"get_holdings", "get_nav"}


@pytest.mark.asyncio
async def test_get_tool():
    from synapse.mcp.registry import MCPRegistry
    registry = MCPRegistry()
    tools = [MCPToolDefinition(name="get_holdings", description="H", input_schema={}, server_id="kite")]
    await registry.register_adapter(FakeAdapter("kite", tools))
    result = registry.get_tool("get_holdings")
    assert result is not None
    adapter, tool_def = result
    assert tool_def.name == "get_holdings"


@pytest.mark.asyncio
async def test_get_tool_not_found():
    from synapse.mcp.registry import MCPRegistry
    registry = MCPRegistry()
    assert registry.get_tool("nonexistent") is None


@pytest.mark.asyncio
async def test_capabilities_from_tools():
    from synapse.mcp.registry import MCPRegistry
    registry = MCPRegistry()
    tools = [
        MCPToolDefinition(name="get_holdings", description="Fetch holdings", input_schema={}, server_id="kite"),
        MCPToolDefinition(name="get_positions", description="Fetch positions", input_schema={}, server_id="kite"),
    ]
    await registry.register_adapter(FakeAdapter("kite", tools))
    caps = registry.capabilities_from_tools()
    assert len(caps) == 2
    assert caps[0].action == "mcp.kite.get_holdings"
    assert caps[0].family == "mcp.kite"
    assert caps[0].description == "Fetch holdings"
