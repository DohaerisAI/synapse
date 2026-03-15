"""Tests for tool registry — RED phase."""
from __future__ import annotations

import pytest

from synapse.tools.registry import ToolContext, ToolDef, ToolRegistry, ToolResult


# --- ToolDef ---


def _noop_tool(params: dict, *, ctx: ToolContext) -> ToolResult:
    return ToolResult(output="ok")


def _make_tool(name: str = "test_tool", *, needs_approval: bool = False, category: str = "builtin") -> ToolDef:
    return ToolDef(
        name=name,
        description=f"A {name} tool",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        execute=_noop_tool,
        needs_approval=needs_approval,
        category=category,
    )


class TestToolDef:
    def test_frozen(self):
        t = _make_tool()
        with pytest.raises(AttributeError):
            t.name = "other"  # type: ignore[misc]

    def test_to_llm_schema(self):
        t = _make_tool("my_tool")
        schema = t.to_llm_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "my_tool"
        assert schema["function"]["description"] == "A my_tool tool"
        assert schema["function"]["parameters"] == t.input_schema


class TestToolResult:
    def test_success_result(self):
        r = ToolResult(output="data here")
        assert r.output == "data here"
        assert r.error is None
        assert r.artifacts is None

    def test_error_result(self):
        r = ToolResult(output="", error="boom")
        assert r.error == "boom"

    def test_frozen(self):
        r = ToolResult(output="x")
        with pytest.raises(AttributeError):
            r.output = "y"  # type: ignore[misc]


# --- ToolRegistry ---


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        t = _make_tool("alpha")
        reg.register(t)
        assert reg.get("alpha") is t

    def test_get_missing_returns_none(self):
        reg = ToolRegistry()
        assert reg.get("nope") is None

    def test_register_duplicate_raises(self):
        reg = ToolRegistry()
        reg.register(_make_tool("dup"))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_make_tool("dup"))

    def test_unregister(self):
        reg = ToolRegistry()
        reg.register(_make_tool("removeme"))
        reg.unregister("removeme")
        assert reg.get("removeme") is None

    def test_unregister_missing_is_noop(self):
        reg = ToolRegistry()
        reg.unregister("ghost")  # should not raise

    def test_all_tools(self):
        reg = ToolRegistry()
        reg.register(_make_tool("a"))
        reg.register(_make_tool("b"))
        reg.register(_make_tool("c"))
        names = [t.name for t in reg.all_tools()]
        assert set(names) == {"a", "b", "c"}

    def test_all_tools_empty(self):
        reg = ToolRegistry()
        assert reg.all_tools() == []

    def test_tool_schemas_for_llm(self):
        reg = ToolRegistry()
        reg.register(_make_tool("x"))
        reg.register(_make_tool("y"))
        schemas = reg.tool_schemas_for_llm()
        assert len(schemas) == 2
        names = {s["function"]["name"] for s in schemas}
        assert names == {"x", "y"}
        for s in schemas:
            assert s["type"] == "function"
            assert "parameters" in s["function"]

    def test_tool_schemas_empty(self):
        reg = ToolRegistry()
        assert reg.tool_schemas_for_llm() == []

    def test_register_mcp_tools(self):
        reg = ToolRegistry()
        fake_mcp_tools = [
            {"name": "get_holdings", "description": "Get holdings", "input_schema": {"type": "object"}},
            {"name": "place_order", "description": "Place order", "input_schema": {"type": "object"}},
        ]
        count = reg.register_mcp_tools("kite", fake_mcp_tools, execute_fn=_noop_tool)
        assert count == 2
        assert reg.get("kite.get_holdings") is not None
        assert reg.get("kite.place_order") is not None
        assert reg.get("kite.get_holdings").category == "mcp.kite"

    def test_register_mcp_tools_prefixed_names(self):
        reg = ToolRegistry()
        fake_mcp_tools = [
            {"name": "analyze", "description": "Analyze", "input_schema": {}},
        ]
        reg.register_mcp_tools("tradingview", fake_mcp_tools, execute_fn=_noop_tool)
        t = reg.get("tradingview.analyze")
        assert t is not None
        schema = t.to_llm_schema()
        assert schema["function"]["name"] == "tradingview.analyze"

    def test_register_skill_tools(self):
        reg = ToolRegistry()
        skill_tools = [
            {"name": "analyze_portfolio", "description": "Analyze portfolio risk", "parameters": {"type": "object"}},
        ]
        count = reg.register_skill_tools("finance", skill_tools, execute_fn=_noop_tool)
        assert count == 1
        t = reg.get("skill.finance.analyze_portfolio")
        assert t is not None
        assert t.category == "skill.finance"

    def test_tools_by_category(self):
        reg = ToolRegistry()
        reg.register(_make_tool("a", category="builtin"))
        reg.register(_make_tool("b", category="gws"))
        reg.register(_make_tool("c", category="builtin"))
        builtins = reg.tools_by_category("builtin")
        assert len(builtins) == 2
        assert {t.name for t in builtins} == {"a", "c"}
