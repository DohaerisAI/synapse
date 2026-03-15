"""MCP type models — immutable Pydantic models for the MCP layer."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MCPAuth(BaseModel, frozen=True):
    """Authentication configuration for an MCP server connection."""
    auth_type: str = "none"
    token: str = ""
    refresh_url: str = ""
    scopes: list[str] = Field(default_factory=list)


class MCPToolDefinition(BaseModel, frozen=True):
    """A tool exposed by an MCP server."""
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    server_id: str = ""


class MCPToolResult(BaseModel, frozen=True):
    """Result of calling an MCP tool."""
    tool_name: str
    success: bool
    data: Any = None
    error: str | None = None
    latency_ms: float = 0.0


class MCPConnectionStatus(StrEnum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"
    CONNECTING = "CONNECTING"


class MCPConnectionInfo(BaseModel, frozen=True):
    """Status snapshot for a connected MCP server."""
    server_id: str
    url: str
    status: MCPConnectionStatus
    tool_count: int = 0
    last_health_check: str | None = None
