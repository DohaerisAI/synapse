"""Finance executor — routes finance.* actions to MCP tool calls."""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from ..models import ExecutionResult, PlannedAction

if TYPE_CHECKING:
    from ..mcp.registry import MCPRegistry

logger = logging.getLogger(__name__)

# Mapping: finance capability → (server_id, mcp_tool_name)
# This is data-driven — no hardcoded keyword matching for routing.
_CAPABILITY_TO_MCP: dict[str, tuple[str, str]] = {
    "finance.holdings.read": ("kite", "get_holdings"),
    "finance.positions.read": ("kite", "get_positions"),
    "finance.margins.read": ("kite", "get_margins"),
    "finance.mf.holdings": ("kite", "get_mf_holdings"),
    "finance.mf.nav_history": ("mfapi", "get_nav_history"),
    "finance.mf.sip_xirr": ("mfapi", "calculate_xirr"),
    "finance.technical.analyze": ("tradingview", "get_analysis"),
    "finance.technical.scan": ("tradingview", "get_screener"),
    "finance.chart.capture": ("tradingview_chart", "capture_chart"),
    "finance.chart.analyze": ("tradingview_chart", "analyze_chart"),
    "finance.sentiment.analyze": ("tradingview", "get_sentiment"),
    "finance.macro.summary": ("tradingview", "get_calendar"),
    "finance.portfolio.summary": ("kite", "get_holdings"),
    "finance.portfolio.risk": ("kite", "get_holdings"),
    "finance.trade.suggest": ("tradingview", "get_analysis"),
    "finance.trade.gtt_place": ("kite", "place_gtt"),
}


class FinanceExecutor:
    """Routes finance.* actions to MCP tool calls via MCPRegistry.

    All routing is data-driven via _CAPABILITY_TO_MCP mapping.
    No hardcoded keyword matching — just a dict lookup.
    """

    def __init__(self, mcp_registry: MCPRegistry) -> None:
        self._registry = mcp_registry

    async def execute(self, action: PlannedAction) -> ExecutionResult:
        """Execute a finance action by calling the mapped MCP tool."""
        mapping = _CAPABILITY_TO_MCP.get(action.action)
        if mapping is None:
            return ExecutionResult(
                action=action.action,
                success=False,
                detail=f"unsupported finance action: {action.action}",
            )

        server_id, tool_name = mapping
        adapter = self._registry.get(server_id)
        if adapter is None:
            return ExecutionResult(
                action=action.action,
                success=False,
                detail=f"MCP server '{server_id}' not connected — required for {action.action}",
            )

        try:
            result = await adapter.call_tool(tool_name, action.payload)
        except Exception as e:
            logger.exception("MCP call failed: %s.%s", server_id, tool_name)
            return ExecutionResult(
                action=action.action,
                success=False,
                detail=f"MCP call failed: {server_id}.{tool_name} — {e}",
            )

        if not result.success:
            return ExecutionResult(
                action=action.action,
                success=False,
                detail=result.error or f"{server_id}.{tool_name} returned error",
            )

        return ExecutionResult(
            action=action.action,
            success=True,
            detail=f"{action.action} via {server_id}.{tool_name}",
            artifacts={"output": result.data, "latency_ms": result.latency_ms},
        )
