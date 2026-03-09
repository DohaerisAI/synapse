"""MCP registry — manages MCP server connections and tool discovery."""
from __future__ import annotations

import logging
from typing import Any, Protocol

from ..capabilities import CapabilityDefinition
from .types import MCPConnectionInfo, MCPConnectionStatus, MCPToolDefinition

logger = logging.getLogger(__name__)


class MCPAdapterLike(Protocol):
    """Protocol for objects that behave like MCPAdapter."""
    server_id: str

    @property
    def connected(self) -> bool: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def list_tools(self) -> list[MCPToolDefinition]: ...
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...
    async def health_check(self) -> bool: ...


class MCPRegistry:
    """Central hub for all MCP server connections."""

    def __init__(self) -> None:
        self._adapters: dict[str, MCPAdapterLike] = {}
        self._tools: dict[str, tuple[MCPAdapterLike, MCPToolDefinition]] = {}

    async def register_adapter(self, adapter: MCPAdapterLike) -> MCPConnectionInfo:
        """Register and connect an MCP adapter, then discover its tools."""
        if not adapter.connected:
            await adapter.connect()
        tools = await adapter.list_tools()
        self._adapters[adapter.server_id] = adapter
        for tool in tools:
            self._tools[tool.name] = (adapter, tool)
        logger.info("MCP registered: %s with %d tools", adapter.server_id, len(tools))
        return MCPConnectionInfo(
            server_id=adapter.server_id,
            url=getattr(adapter, "url", ""),
            status=MCPConnectionStatus.CONNECTED if adapter.connected else MCPConnectionStatus.DISCONNECTED,
            tool_count=len(tools),
        )

    async def unregister(self, server_id: str) -> None:
        """Disconnect and remove an MCP adapter."""
        adapter = self._adapters.pop(server_id, None)
        if adapter is None:
            return
        # Remove tools belonging to this adapter
        self._tools = {
            name: (a, t) for name, (a, t) in self._tools.items()
            if a.server_id != server_id
        }
        await adapter.disconnect()
        logger.info("MCP unregistered: %s", server_id)

    def get(self, server_id: str) -> MCPAdapterLike | None:
        """Get adapter by server ID."""
        return self._adapters.get(server_id)

    def get_tool(self, tool_name: str) -> tuple[MCPAdapterLike, MCPToolDefinition] | None:
        """Look up a tool across all connected servers."""
        return self._tools.get(tool_name)

    def list_connected(self) -> list[MCPConnectionInfo]:
        """List all connected MCP servers."""
        result: list[MCPConnectionInfo] = []
        for server_id, adapter in self._adapters.items():
            tool_count = sum(1 for _, (a, _) in self._tools.items() if a.server_id == server_id)
            result.append(MCPConnectionInfo(
                server_id=server_id,
                url=getattr(adapter, "url", ""),
                status=MCPConnectionStatus.CONNECTED if adapter.connected else MCPConnectionStatus.DISCONNECTED,
                tool_count=tool_count,
            ))
        return result

    async def discover_all_tools(self) -> list[MCPToolDefinition]:
        """Aggregate tools from all connected servers."""
        all_tools: list[MCPToolDefinition] = []
        for adapter in self._adapters.values():
            if adapter.connected:
                tools = await adapter.list_tools()
                all_tools.extend(tools)
        # Update internal cache
        self._tools = {}
        for tool in all_tools:
            adapter = self._adapters.get(tool.server_id)
            if adapter is not None:
                self._tools[tool.name] = (adapter, tool)
        return all_tools

    def capabilities_from_tools(self) -> list[CapabilityDefinition]:
        """Generate CapabilityDefinition entries from discovered MCP tools.

        Convention: action = "mcp.{server_id}.{tool_name}"
                    family = "mcp.{server_id}"
        """
        caps: list[CapabilityDefinition] = []
        for tool_name, (adapter, tool) in self._tools.items():
            caps.append(CapabilityDefinition(
                action=f"mcp.{adapter.server_id}.{tool_name}",
                family=f"mcp.{adapter.server_id}",
                description=tool.description,
            ))
        return caps
