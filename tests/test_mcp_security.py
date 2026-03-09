"""Tests for MCP security — scope checking, rate limiting, audit logging."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from synapse.store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "test.db")
    s.initialize()
    return s


def test_rate_limiter_allows_within_limit():
    from synapse.mcp.security import MCPRateLimiter
    limiter = MCPRateLimiter(max_calls_per_minute=5)
    for _ in range(5):
        assert limiter.allow("kite") is True


def test_rate_limiter_blocks_excess():
    from synapse.mcp.security import MCPRateLimiter
    limiter = MCPRateLimiter(max_calls_per_minute=3)
    for _ in range(3):
        limiter.allow("kite")
    assert limiter.allow("kite") is False


def test_rate_limiter_independent_per_server():
    from synapse.mcp.security import MCPRateLimiter
    limiter = MCPRateLimiter(max_calls_per_minute=2)
    assert limiter.allow("kite") is True
    assert limiter.allow("kite") is True
    assert limiter.allow("kite") is False
    # Different server should still be allowed
    assert limiter.allow("mfapi") is True


def test_rate_limiter_reset_after_window():
    from synapse.mcp.security import MCPRateLimiter
    limiter = MCPRateLimiter(max_calls_per_minute=1)
    assert limiter.allow("kite") is True
    assert limiter.allow("kite") is False
    limiter.reset("kite")
    assert limiter.allow("kite") is True


def test_audit_logger_writes_to_store(store):
    from synapse.mcp.security import MCPAuditLogger
    logger = MCPAuditLogger(store)
    logger.log_call(server_id="kite", tool_name="get_holdings", success=True, latency_ms=50.0)
    logger.log_call(server_id="kite", tool_name="place_gtt", success=False, latency_ms=200.0, error="timeout")
    calls = store.list_mcp_calls(server_id="kite")
    assert len(calls) == 2


def test_scope_checker_allows_matching_scope():
    from synapse.mcp.security import MCPScopeChecker
    checker = MCPScopeChecker()
    checker.register_scopes("kite", ["read", "write"])
    assert checker.check("kite", "read") is True
    assert checker.check("kite", "write") is True


def test_scope_checker_denies_missing_scope():
    from synapse.mcp.security import MCPScopeChecker
    checker = MCPScopeChecker()
    checker.register_scopes("kite", ["read"])
    assert checker.check("kite", "write") is False


def test_scope_checker_allows_all_when_no_scopes_registered():
    """If no scopes are registered for a server, all operations are allowed."""
    from synapse.mcp.security import MCPScopeChecker
    checker = MCPScopeChecker()
    assert checker.check("kite", "anything") is True
