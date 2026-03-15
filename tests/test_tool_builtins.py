"""Tests for builtin tools — RED → GREEN."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synapse.tools.registry import ToolContext, ToolRegistry, ToolResult


def _make_ctx(**overrides) -> ToolContext:
    memory = MagicMock()
    store = MagicMock()
    config = MagicMock()
    defaults = dict(
        session_key="sess-1",
        user_id="user-1",
        memory=memory,
        store=store,
        config=config,
    )
    defaults.update(overrides)
    return ToolContext(**defaults)


@pytest.fixture
def registry() -> ToolRegistry:
    from synapse.tools.builtins import register_builtin_tools

    reg = ToolRegistry()
    register_builtin_tools(reg)
    return reg


class TestBuiltinToolsRegistered:
    def test_memory_tools_exist(self, registry: ToolRegistry):
        for name in ("memory_read", "memory_write", "memory_delete", "memory_search"):
            assert registry.get(name) is not None, f"missing tool: {name}"

    def test_self_tools_exist(self, registry: ToolRegistry):
        for name in ("self_describe", "self_health", "self_capabilities", "diagnosis_report"):
            assert registry.get(name) is not None, f"missing tool: {name}"

    def test_web_tools_exist(self, registry: ToolRegistry):
        for name in ("web_search", "web_fetch"):
            assert registry.get(name) is not None, f"missing tool: {name}"

    def test_shell_exec_exists(self, registry: ToolRegistry):
        assert registry.get("shell_exec") is not None

    def test_load_skill_exists(self, registry: ToolRegistry):
        assert registry.get("load_skill") is not None

    def test_reminder_create_exists(self, registry: ToolRegistry):
        assert registry.get("reminder_create") is not None


class TestMemoryRead:
    @pytest.mark.asyncio
    async def test_read_user_memory(self, registry: ToolRegistry):
        ctx = _make_ctx()
        ctx.memory.read_user_memory.return_value = "user prefs here"
        tool = registry.get("memory_read")
        result = await tool.execute({"scope": "user"}, ctx=ctx)
        assert isinstance(result, ToolResult)
        assert result.error is None
        assert "user prefs here" in result.output

    @pytest.mark.asyncio
    async def test_read_session_memory(self, registry: ToolRegistry):
        ctx = _make_ctx()
        ctx.memory.read_session_notes.return_value = "session notes"
        ctx.memory.read_session_summary.return_value = "summary"
        ctx.memory.read_recent_transcript.return_value = []
        tool = registry.get("memory_read")
        result = await tool.execute({"scope": "session"}, ctx=ctx)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_read_all_memory(self, registry: ToolRegistry):
        ctx = _make_ctx()
        ctx.memory.read_user_memory.return_value = ""
        ctx.memory.read_session_notes.return_value = ""
        ctx.memory.read_session_summary.return_value = ""
        ctx.memory.read_recent_transcript.return_value = []
        ctx.memory.read_global_memory.return_value = ""
        tool = registry.get("memory_read")
        result = await tool.execute({"scope": "all"}, ctx=ctx)
        assert result.error is None


class TestMemoryWrite:
    @pytest.mark.asyncio
    async def test_write_session(self, registry: ToolRegistry):
        ctx = _make_ctx()
        tool = registry.get("memory_write")
        result = await tool.execute({"scope": "session", "content": "note1"}, ctx=ctx)
        assert result.error is None
        ctx.memory.append_notes.assert_called_once_with("sess-1", "note1")

    @pytest.mark.asyncio
    async def test_write_user(self, registry: ToolRegistry):
        ctx = _make_ctx()
        tool = registry.get("memory_write")
        result = await tool.execute({"scope": "user", "content": "pref"}, ctx=ctx)
        assert result.error is None
        ctx.memory.append_user_memory.assert_called_once_with("user-1", "pref")


class TestMemoryDelete:
    @pytest.mark.asyncio
    async def test_delete_session(self, registry: ToolRegistry):
        ctx = _make_ctx()
        ctx.memory.delete_session_notes.return_value = True
        tool = registry.get("memory_delete")
        result = await tool.execute({"scope": "session", "content": "old note"}, ctx=ctx)
        assert result.error is None
        assert "removed" in result.output


class TestShellExec:
    def test_needs_approval_for_dangerous(self, registry: ToolRegistry):
        tool = registry.get("shell_exec")
        assert tool.check_approval({"command": "rm -rf /"}) is True

    def test_no_approval_for_safe(self, registry: ToolRegistry):
        tool = registry.get("shell_exec")
        assert tool.check_approval({"command": "pwd"}) is False


class TestLoadSkill:
    @pytest.mark.asyncio
    async def test_load_existing_skill(self, registry: ToolRegistry):
        ctx = _make_ctx()
        skill_reg = MagicMock()
        skill_def = MagicMock()
        skill_def.instruction_markdown = "# Finance Skill\nDo finance things."
        skill_def.name = "Finance"
        skill_reg.get.return_value = skill_def
        ctx = _make_ctx(skill_registry=skill_reg)
        tool = registry.get("load_skill")
        result = await tool.execute({"skill_id": "finance"}, ctx=ctx)
        assert result.error is None
        assert "Finance" in result.output

    @pytest.mark.asyncio
    async def test_load_missing_skill(self, registry: ToolRegistry):
        ctx = _make_ctx()
        skill_reg = MagicMock()
        skill_reg.get.return_value = None
        ctx = _make_ctx(skill_registry=skill_reg)
        tool = registry.get("load_skill")
        result = await tool.execute({"skill_id": "nope"}, ctx=ctx)
        assert result.error is not None


class TestSelfDescribe:
    @pytest.mark.asyncio
    async def test_describe(self, registry: ToolRegistry):
        ctx = _make_ctx()
        tool = registry.get("self_describe")
        result = await tool.execute({}, ctx=ctx)
        assert result.error is None
        assert "Synapse" in result.output
