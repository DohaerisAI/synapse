"""Tests for MCPAdapter — RED phase."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from synapse.mcp.types import MCPAuth


class FakeMCPTransport:
    """Fake transport that returns canned MCP JSON-RPC responses."""

    def __init__(self, tools: list[dict[str, Any]] | None = None):
        self.tools = tools or [
            {
                "name": "get_holdings",
                "description": "Fetch equity holdings",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params or {}))
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "serverInfo": {"name": "test"}}
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            tool_name = (params or {}).get("name", "")
            return {"content": [{"type": "text", "text": json.dumps({"result": f"{tool_name}_ok"})}]}
        return {}


@pytest.fixture
def fake_transport():
    return FakeMCPTransport()


@pytest.mark.asyncio
async def test_adapter_connect(fake_transport):
    from synapse.mcp.adapter import MCPAdapter
    adapter = MCPAdapter("test_server", "https://example.com/mcp", MCPAuth())
    adapter._transport = fake_transport  # inject fake
    await adapter.connect()
    assert adapter.connected is True
    assert ("initialize", {}) in [(m, p) for m, p in fake_transport.calls]


@pytest.mark.asyncio
async def test_adapter_list_tools(fake_transport):
    from synapse.mcp.adapter import MCPAdapter
    adapter = MCPAdapter("test_server", "https://example.com/mcp", MCPAuth())
    adapter._transport = fake_transport
    await adapter.connect()
    tools = await adapter.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "get_holdings"
    assert tools[0].server_id == "test_server"


@pytest.mark.asyncio
async def test_adapter_call_tool(fake_transport):
    from synapse.mcp.adapter import MCPAdapter
    adapter = MCPAdapter("test_server", "https://example.com/mcp", MCPAuth())
    adapter._transport = fake_transport
    await adapter.connect()
    result = await adapter.call_tool("get_holdings", {})
    assert result.success is True
    assert result.tool_name == "get_holdings"
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_adapter_disconnect(fake_transport):
    from synapse.mcp.adapter import MCPAdapter
    adapter = MCPAdapter("test_server", "https://example.com/mcp", MCPAuth())
    adapter._transport = fake_transport
    await adapter.connect()
    assert adapter.connected is True
    await adapter.disconnect()
    assert adapter.connected is False


@pytest.mark.asyncio
async def test_adapter_call_tool_not_connected():
    from synapse.mcp.adapter import MCPAdapter
    adapter = MCPAdapter("test_server", "https://example.com/mcp", MCPAuth())
    result = await adapter.call_tool("get_holdings", {})
    assert result.success is False
    assert "not connected" in (result.error or "").lower()
