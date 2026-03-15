"""MCP tools — dynamic discovery and registration from MCP servers."""
from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from .registry import ToolContext, ToolDef, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from ..mcp.adapter import MCPAdapter

# Tool names containing these keywords require approval
_APPROVAL_KEYWORDS = {"place", "order", "trade", "delete", "remove", "send", "create_order"}


def _mcp_approval_policy(server_id: str, tool_name: str) -> bool:
    """Default approval policy for MCP tools."""
    lowered = tool_name.lower()
    return any(kw in lowered for kw in _APPROVAL_KEYWORDS)


def _make_mcp_tool_fn(adapter: "MCPAdapter", tool_name: str):
    """Create an async tool function bound to an MCP tool call."""

    async def _execute(params: dict[str, Any], *, ctx: Any) -> ToolResult:
        result = await adapter.call_tool(tool_name, params)
        if not result.success:
            return ToolResult(
                output="",
                error=result.error or f"{tool_name} returned error",
            )
        output = json.dumps(result.data, indent=2, default=str) if result.data is not None else "ok"
        return ToolResult(
            output=output,
            artifacts={"latency_ms": result.latency_ms},
        )

    return _execute


async def register_mcp_server_tools(
    registry: ToolRegistry,
    server_id: str,
    adapter: "MCPAdapter",
) -> int:
    """Discover all tools from an MCP server and register them.

    Tool names are prefixed with server_id: e.g. ``kite.get_holdings``.
    Returns the count of tools registered.
    """
    tools = await adapter.list_tools()
    count = 0
    for tool in tools:
        name = f"{server_id}.{tool.name}"
        needs_approval = _mcp_approval_policy(server_id, tool.name)
        registry.register(
            ToolDef(
                name=name,
                description=tool.description,
                input_schema=tool.input_schema,
                execute=_make_mcp_tool_fn(adapter, tool.name),
                needs_approval=needs_approval,
                category=f"mcp.{server_id}",
            )
        )
        count += 1
    return count
