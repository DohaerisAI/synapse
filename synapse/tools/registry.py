"""Tool registry — ToolDef, ToolResult, ToolContext, and ToolRegistry."""
from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import threading
import time
from typing import Any, Callable, TYPE_CHECKING

from ..models import utc_now

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..skill_runtime import CommandRunner
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
    run_id: str | None = None
    delivery_target: Any = None
    job_service: Any = None
    job_id: str | None = None
    approval_id: str | None = None
    cancel_event: Any = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    mcp_registry: MCPRegistry | None = None
    skill_registry: SkillRegistry | None = None
    command_runner: CommandRunner | None = None
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

    def __post_init__(self) -> None:
        original_execute = self.execute
        if getattr(original_execute, "_synapse_tool_wrapped", False):
            return

        async def _wrapped_execute(params: dict[str, Any], *, ctx=None):
            started_at = utc_now().isoformat()
            started_perf = time.perf_counter()
            store = getattr(ctx, "store", None)
            needs_approval = self.check_approval(params)
            status = "ok"
            error: str | None = None
            try:
                result = original_execute(params, ctx=ctx)
                if inspect.isawaitable(result):
                    result = await result
                if getattr(result, "error", None):
                    status = "error"
                    error = str(result.error)
                return result
            except Exception as exc:
                status = "error"
                error = str(exc)
                raise
            finally:
                if store is not None:
                    finished_at = utc_now().isoformat()
                    duration_ms = int((time.perf_counter() - started_perf) * 1000)
                    store.append_tool_event(
                        run_id=getattr(ctx, "run_id", None),
                        session_key=getattr(ctx, "session_key", None),
                        job_id=getattr(ctx, "job_id", None),
                        tool_name=self.name,
                        needs_approval=needs_approval,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                        status=status,
                        error=error,
                    )

        setattr(_wrapped_execute, "_synapse_tool_wrapped", True)
        object.__setattr__(self, "execute", _wrapped_execute)

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
        self._lock = threading.RLock()

    def register(self, tool: ToolDef) -> None:
        """Register a tool. Raises ValueError if name already exists."""
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"tool '{tool.name}' already registered")
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool by name. No-op if not found."""
        with self._lock:
            self._tools.pop(name, None)

    def get(self, name: str) -> ToolDef | None:
        """Lookup a single tool by name."""
        with self._lock:
            return self._tools.get(name)

    def all_tools(self) -> list[ToolDef]:
        """Return all registered tools."""
        with self._lock:
            return list(self._tools.values())

    def tool_schemas_for_llm(self) -> list[dict[str, Any]]:
        """Return OpenAI function-calling format schemas for all tools."""
        return [tool.to_llm_schema() for tool in self.all_tools()]

    def tools_by_category(self, category: str) -> list[ToolDef]:
        """Return tools filtered by category."""
        return [t for t in self.all_tools() if t.category == category]

    def replace_tools(
        self,
        *,
        predicate: Callable[[ToolDef], bool],
        replacements: list[ToolDef],
    ) -> None:
        """Atomically replace a subset of tools selected by predicate."""
        with self._lock:
            next_tools = {
                name: tool
                for name, tool in self._tools.items()
                if not predicate(tool)
            }
            replacement_names: set[str] = set()
            for tool in replacements:
                if tool.name in replacement_names:
                    raise ValueError(f"duplicate replacement tool '{tool.name}'")
                if tool.name in next_tools:
                    raise ValueError(f"tool '{tool.name}' already registered")
                replacement_names.add(tool.name)
                next_tools[tool.name] = tool
            self._tools = next_tools

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
