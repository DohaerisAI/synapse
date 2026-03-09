"""MCP security — scope checking, rate limiting, audit logging."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..store import SQLiteStore


class MCPScopeChecker:
    """Validates that tool calls are within allowed scopes for a server."""

    def __init__(self) -> None:
        self._scopes: dict[str, set[str]] = {}

    def register_scopes(self, server_id: str, scopes: list[str]) -> None:
        self._scopes[server_id] = set(scopes)

    def check(self, server_id: str, scope: str) -> bool:
        """Return True if the scope is allowed for this server.

        If no scopes are registered, all operations are allowed.
        """
        registered = self._scopes.get(server_id)
        if registered is None:
            return True
        return scope in registered


class MCPRateLimiter:
    """Token-bucket rate limiter per server_id."""

    def __init__(self, max_calls_per_minute: int = 60) -> None:
        self._max = max_calls_per_minute
        self._buckets: dict[str, list[float]] = {}

    def allow(self, server_id: str) -> bool:
        """Return True if the call is within rate limits."""
        now = time.monotonic()
        window = 60.0
        bucket = self._buckets.setdefault(server_id, [])
        # Prune expired entries
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True

    def reset(self, server_id: str) -> None:
        """Reset the rate limit bucket for a server."""
        self._buckets.pop(server_id, None)


class MCPAuditLogger:
    """Logs every MCP tool call to the store for audit trail."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def log_call(
        self,
        *,
        server_id: str,
        tool_name: str,
        success: bool,
        latency_ms: float,
        error: str | None = None,
    ) -> None:
        self._store.log_mcp_call(
            server_id=server_id,
            tool_name=tool_name,
            success=success,
            latency_ms=latency_ms,
            error=error,
        )
