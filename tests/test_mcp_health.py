"""Tests for MCP health monitor."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from synapse.mcp.types import MCPToolDefinition


class FakeHealthAdapter:
    def __init__(self, server_id: str, *, healthy: bool = True):
        self.server_id = server_id
        self.url = f"https://fake-{server_id}.com/mcp"
        self._connected = True
        self._healthy = healthy
        self.health_check_count = 0
        self.reconnect_count = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True
        self.reconnect_count += 1

    async def disconnect(self) -> None:
        self._connected = False

    async def list_tools(self) -> list[MCPToolDefinition]:
        return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        pass

    async def health_check(self) -> bool:
        self.health_check_count += 1
        return self._healthy


@pytest.mark.asyncio
async def test_health_monitor_checks_adapters():
    from synapse.mcp.health import MCPHealthMonitor
    from synapse.mcp.registry import MCPRegistry

    registry = MCPRegistry()
    adapter = FakeHealthAdapter("kite")
    registry._adapters = {"kite": adapter}

    monitor = MCPHealthMonitor(registry, check_interval=0.1)
    await monitor.check_all()
    assert adapter.health_check_count == 1


@pytest.mark.asyncio
async def test_health_monitor_reconnects_unhealthy():
    from synapse.mcp.health import MCPHealthMonitor
    from synapse.mcp.registry import MCPRegistry

    registry = MCPRegistry()
    adapter = FakeHealthAdapter("kite", healthy=False)
    adapter._connected = False
    registry._adapters = {"kite": adapter}

    monitor = MCPHealthMonitor(registry, check_interval=0.1)
    await monitor.check_all()
    # Should have attempted reconnect
    assert adapter.reconnect_count >= 1


@pytest.mark.asyncio
async def test_health_monitor_start_stop():
    from synapse.mcp.health import MCPHealthMonitor
    from synapse.mcp.registry import MCPRegistry

    registry = MCPRegistry()
    adapter = FakeHealthAdapter("kite")
    registry._adapters = {"kite": adapter}

    monitor = MCPHealthMonitor(registry, check_interval=0.05)
    await monitor.start()
    await asyncio.sleep(0.15)
    await monitor.stop()
    # Should have run at least one check
    assert adapter.health_check_count >= 1
