"""MCP adapter — client for a single MCP server."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from .types import MCPAuth, MCPToolDefinition, MCPToolResult

logger = logging.getLogger(__name__)


class MCPTransport(Protocol):
    """Protocol for MCP JSON-RPC transport."""

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any: ...


class MCPAdapter:
    """Client adapter for a single MCP server.

    Manages connection lifecycle, tool discovery, and tool invocation
    via a pluggable transport (HTTP+SSE, stdio, etc.).
    """

    def __init__(self, server_id: str, url: str, auth: MCPAuth) -> None:
        self.server_id = server_id
        self.url = url
        self.auth = auth
        self._connected = False
        self._transport: MCPTransport | None = None
        self._tools: list[MCPToolDefinition] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the MCP server and perform initialization handshake."""
        if self._transport is None:
            raise RuntimeError(f"no transport configured for MCP server {self.server_id}")
        # Use transport's initialize() if available (handles full handshake),
        # otherwise fall back to raw send
        if hasattr(self._transport, "initialize"):
            await self._transport.initialize()
        else:
            await self._transport.send("initialize")
        self._connected = True
        logger.info("MCP adapter connected: %s (%s)", self.server_id, self.url)

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        self._connected = False
        self._tools = []
        logger.info("MCP adapter disconnected: %s", self.server_id)

    async def list_tools(self) -> list[MCPToolDefinition]:
        """Discover available tools from the MCP server."""
        if not self._connected or self._transport is None:
            return []
        response = await self._transport.send("tools/list")
        raw_tools = response.get("tools", []) if isinstance(response, dict) else []
        self._tools = [
            MCPToolDefinition(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_id=self.server_id,
            )
            for t in raw_tools
            if isinstance(t, dict)
        ]
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Call a tool on the MCP server."""
        if not self._connected or self._transport is None:
            return MCPToolResult(
                tool_name=tool_name,
                success=False,
                error=f"MCP server {self.server_id} is not connected",
            )
        start = time.monotonic()
        try:
            response = await self._transport.send("tools/call", {"name": tool_name, "arguments": arguments})
            latency_ms = (time.monotonic() - start) * 1000
            # Parse MCP tool result content
            content = response.get("content", []) if isinstance(response, dict) else []
            data = self._extract_content(content)
            return MCPToolResult(
                tool_name=tool_name,
                success=True,
                data=data,
                latency_ms=latency_ms,
            )
        except Exception as e:
            latency_ms = (time.monotonic() - start) * 1000
            return MCPToolResult(
                tool_name=tool_name,
                success=False,
                error=str(e),
                latency_ms=latency_ms,
            )

    async def health_check(self) -> bool:
        """Lightweight connectivity check."""
        return self._connected

    def _extract_content(self, content: list[Any]) -> Any:
        """Extract data from MCP content blocks."""
        if not content:
            return None
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text
        return content
