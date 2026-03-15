"""MCP (Model Context Protocol) client subsystem."""
from __future__ import annotations

from .types import (
    MCPAuth,
    MCPConnectionInfo,
    MCPConnectionStatus,
    MCPToolDefinition,
    MCPToolResult,
)

from .transport import HttpMcpTransport
from .stdio_transport import StdioMcpTransport

__all__ = [
    "HttpMcpTransport",
    "StdioMcpTransport",
    "MCPAuth",
    "MCPConnectionInfo",
    "MCPConnectionStatus",
    "MCPToolDefinition",
    "MCPToolResult",
]
