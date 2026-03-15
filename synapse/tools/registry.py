"""Tool registry — ToolDef, ToolResult, ToolContext, and ToolRegistry."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..memory import MemoryStore
    from ..mcp.registry import MCPRegistry
    from ..skills import SkillRegistry
    from ..store import SQLiteStore


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Immutable result returned by a tool execution."""

    output: str
    error: str | None = None
    artifacts: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Context passed to every tool execution — provides access to runtime services."""

    session_key: str
    user_id: str
    memory: MemoryStore
    store: SQLiteStore
    config: AppConfig
    mcp_registry: MCPRegistry | None = None
    skill_registry: SkillRegistry | None = None
    diagnosis_engine: Any = None


@dataclass(frozen=True, slots=True)
class ToolDef:
    """Immutable tool definition — name, schema, execute function, and approval policy."""

    name: str
    description: str
    input_schema: dict[str, Any]
    execute: Callable[..., Any]
    needs_approval: bool | Callable[[dict[str, Any]], bool] = False
    category: str = "builtin"

    def to_llm_schema(self) -> dict[str, Any]:
        """Return OpenAI function-calling format schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def check_approval(self, params: dict[str, Any]) -> bool:
        """Return True if this tool call requires user approval."""
        if callable(self.needs_approval):
            return self.needs_approval(params)
        return bool(self.needs_approval)


class ToolRegistry:
    """Registry of available tools — supports builtin, MCP, and skill tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        """Register a tool. Raises ValueError if name already exists."""
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool by name. No-op if not found."""
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolDef | None:
        """Lookup a single tool by name."""
        return self._tools.get(name)

    def all_tools(self) -> list[ToolDef]:
        """Return all registered tools."""
        return list(self._tools.values())

    def tool_schemas_for_llm(self) -> list[dict[str, Any]]:
        """Return OpenAI function-calling format schemas for all tools."""
        return [tool.to_llm_schema() for tool in self._tools.values()]

    def tools_by_category(self, category: str) -> list[ToolDef]:
        """Return tools filtered by category."""
        return [t for t in self._tools.values() if t.category == category]

    def register_mcp_tools(
        self,
        server_id: str,
        tools: list[dict[str, Any]],
        *,
        execute_fn: Callable[..., Any],
        approval_policy: Callable[[str, str], bool] | None = None,
    ) -> int:
        """Bulk register tools from an MCP server discovery response.

        Tool names are prefixed with server_id: e.g. ``kite.get_holdings``.
        Returns the count of tools registered.
        """
        count = 0
        for tool in tools:
            name = f"{server_id}.{tool['name']}"
            needs_approval: bool | Callable[[dict[str, Any]], bool] = False
            if approval_policy is not None:
                needs_approval = approval_policy(server_id, tool["name"])
            self.register(
                ToolDef(
                    name=name,
                    description=tool.get("description", ""),
                    input_schema=tool.get("input_schema", {}),
                    execute=execute_fn,
                    needs_approval=needs_approval,
                    category=f"mcp.{server_id}",
                )
            )
            count += 1
        return count

    def register_skill_tools(
        self,
        skill_id: str,
        tools: list[dict[str, Any]],
        *,
        execute_fn: Callable[..., Any],
    ) -> int:
        """Register tools defined in a skill manifest.

        Tool names are prefixed: ``skill.<skill_id>.<tool_name>``.
        Returns the count of tools registered.
        """
        count = 0
        for tool in tools:
            name = f"skill.{skill_id}.{tool['name']}"
            self.register(
                ToolDef(
                    name=name,
                    description=tool.get("description", ""),
                    input_schema=tool.get("parameters", {}),
                    execute=execute_fn,
                    needs_approval=False,
                    category=f"skill.{skill_id}",
                )
            )
            count += 1
        return count
