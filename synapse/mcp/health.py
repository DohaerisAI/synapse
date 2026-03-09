"""MCP health monitor — periodic health checks with auto-reconnect."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import MCPRegistry

logger = logging.getLogger(__name__)


class MCPHealthMonitor:
    """Background health monitor for MCP connections.

    Periodically checks all connected adapters and attempts
    reconnection on failure with exponential backoff.
    """

    def __init__(self, registry: MCPRegistry, check_interval: float = 60.0) -> None:
        self._registry = registry
        self._check_interval = check_interval
        self._task: asyncio.Task[None] | None = None
        self._max_retries = 3

    async def start(self) -> None:
        """Start the background health check loop."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background health check loop."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def check_all(self) -> None:
        """Run a single health check across all adapters."""
        for server_id, adapter in list(self._registry._adapters.items()):
            try:
                healthy = await adapter.health_check()
                if not healthy:
                    logger.warning("MCP server %s unhealthy, attempting reconnect", server_id)
                    await self._reconnect(adapter)
            except Exception:
                logger.exception("Health check failed for %s", server_id)
                await self._reconnect(adapter)

    async def _reconnect(self, adapter: object) -> None:
        """Attempt to reconnect an adapter with backoff."""
        for attempt in range(self._max_retries):
            try:
                await adapter.connect()  # type: ignore[attr-defined]
                logger.info("Reconnected MCP server %s on attempt %d", getattr(adapter, "server_id", "?"), attempt + 1)
                return
            except Exception:
                backoff = 2 ** attempt
                logger.warning("Reconnect attempt %d failed, waiting %ds", attempt + 1, backoff)
                await asyncio.sleep(min(backoff, 10))

    async def _loop(self) -> None:
        """Run health checks in a loop until cancelled."""
        try:
            while True:
                await self.check_all()
                await asyncio.sleep(self._check_interval)
        except asyncio.CancelledError:
            pass
