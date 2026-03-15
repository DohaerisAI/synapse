"""Tests for MCP store schema and methods — RED phase."""
from __future__ import annotations

from pathlib import Path

import pytest

from synapse.store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "test.db")
    s.initialize()
    return s


def test_upsert_and_list_mcp_connections(store):
    store.upsert_mcp_connection(
        server_id="kite",
        url="https://mcp.kite.trade/mcp",
        auth_type="oauth",
        status="connected",
        tool_count=5,
    )
    connections = store.list_mcp_connections()
    assert len(connections) == 1
    assert connections[0]["server_id"] == "kite"
    assert connections[0]["tool_count"] == 5


def test_upsert_mcp_connection_updates(store):
    store.upsert_mcp_connection(
        server_id="kite", url="https://mcp.kite.trade/mcp",
        auth_type="oauth", status="connected", tool_count=5,
    )
    store.upsert_mcp_connection(
        server_id="kite", url="https://mcp.kite.trade/mcp",
        auth_type="oauth", status="error", tool_count=5,
    )
    connections = store.list_mcp_connections()
    assert len(connections) == 1
    assert connections[0]["status"] == "error"


def test_delete_mcp_connection(store):
    store.upsert_mcp_connection(
        server_id="kite", url="https://mcp.kite.trade/mcp",
        auth_type="oauth", status="connected", tool_count=3,
    )
    store.delete_mcp_connection("kite")
    assert store.list_mcp_connections() == []


def test_log_and_list_mcp_calls(store):
    store.log_mcp_call(server_id="kite", tool_name="get_holdings", success=True, latency_ms=123.4)
    store.log_mcp_call(server_id="kite", tool_name="get_positions", success=False, latency_ms=500.0, error="timeout")
    calls = store.list_mcp_calls(server_id="kite")
    assert len(calls) == 2
    assert calls[0]["tool_name"] in {"get_holdings", "get_positions"}


def test_list_mcp_calls_with_limit(store):
    for i in range(10):
        store.log_mcp_call(server_id="kite", tool_name=f"tool_{i}", success=True, latency_ms=1.0)
    calls = store.list_mcp_calls(limit=3)
    assert len(calls) == 3


def test_list_mcp_calls_all_servers(store):
    store.log_mcp_call(server_id="kite", tool_name="t1", success=True, latency_ms=1.0)
    store.log_mcp_call(server_id="mfapi", tool_name="t2", success=True, latency_ms=2.0)
    calls = store.list_mcp_calls()
    assert len(calls) == 2
