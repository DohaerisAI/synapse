"""Tests for MCP type models — RED phase."""
from __future__ import annotations

import pytest


def test_mcp_auth_defaults():
    from synapse.mcp.types import MCPAuth
    auth = MCPAuth()
    assert auth.auth_type == "none"
    assert auth.token == ""
    assert auth.scopes == []


def test_mcp_auth_oauth():
    from synapse.mcp.types import MCPAuth
    auth = MCPAuth(auth_type="oauth", token="tok_123", scopes=["read", "write"])
    assert auth.auth_type == "oauth"
    assert auth.token == "tok_123"
    assert auth.scopes == ["read", "write"]


def test_mcp_tool_definition():
    from synapse.mcp.types import MCPToolDefinition
    tool = MCPToolDefinition(
        name="get_holdings",
        description="Fetch equity holdings",
        input_schema={"type": "object"},
        server_id="kite",
    )
    assert tool.name == "get_holdings"
    assert tool.server_id == "kite"


def test_mcp_tool_result_success():
    from synapse.mcp.types import MCPToolResult
    result = MCPToolResult(tool_name="get_holdings", success=True, data={"holdings": []})
    assert result.success is True
    assert result.error is None
    assert result.latency_ms == 0.0


def test_mcp_tool_result_error():
    from synapse.mcp.types import MCPToolResult
    result = MCPToolResult(tool_name="get_holdings", success=False, data=None, error="timeout")
    assert result.success is False
    assert result.error == "timeout"


def test_mcp_connection_status_values():
    from synapse.mcp.types import MCPConnectionStatus
    assert MCPConnectionStatus.CONNECTED == "CONNECTED"
    assert MCPConnectionStatus.DISCONNECTED == "DISCONNECTED"
    assert MCPConnectionStatus.ERROR == "ERROR"


def test_mcp_connection_info():
    from synapse.mcp.types import MCPConnectionInfo, MCPConnectionStatus
    info = MCPConnectionInfo(
        server_id="kite",
        url="https://mcp.kite.trade/mcp",
        status=MCPConnectionStatus.CONNECTED,
        tool_count=5,
    )
    assert info.server_id == "kite"
    assert info.last_health_check is None


def test_immutability_via_frozen():
    """MCPAuth and other models should be immutable (frozen)."""
    from synapse.mcp.types import MCPAuth
    auth = MCPAuth(auth_type="jwt", token="abc")
    with pytest.raises(Exception):
        auth.token = "xyz"  # type: ignore[misc]
