"""Tests for approval manager — RED → GREEN."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from synapse.approvals import ApprovalManager
from synapse.tools.registry import ToolDef, ToolResult


def _make_tool(name: str, *, needs_approval: bool = True) -> ToolDef:
    return ToolDef(
        name=name,
        description=f"Tool {name}",
        input_schema={},
        execute=AsyncMock(return_value=ToolResult(output="ok")),
        needs_approval=needs_approval,
    )


class TestApprovalManager:
    def test_is_allowed_exact_match(self, tmp_path: Path):
        allow_file = tmp_path / "approvals.json"
        allow_file.write_text(json.dumps({"always_allow": ["gws_gmail_send"]}))
        mgr = ApprovalManager(allow_file)
        assert mgr.is_allowed("gws_gmail_send") is True
        assert mgr.is_allowed("gws_gmail_reply") is False

    def test_is_allowed_glob_pattern(self, tmp_path: Path):
        allow_file = tmp_path / "approvals.json"
        allow_file.write_text(json.dumps({"always_allow": ["gws_*"]}))
        mgr = ApprovalManager(allow_file)
        assert mgr.is_allowed("gws_gmail_send") is True
        assert mgr.is_allowed("gws_calendar_agenda") is True
        assert mgr.is_allowed("shell_exec") is False

    def test_add_to_allowlist(self, tmp_path: Path):
        allow_file = tmp_path / "approvals.json"
        mgr = ApprovalManager(allow_file)
        mgr.add_to_allowlist("shell_exec")
        assert mgr.is_allowed("shell_exec") is True
        # Verify persistence
        reloaded = ApprovalManager(allow_file)
        assert reloaded.is_allowed("shell_exec") is True

    def test_missing_file_creates_empty(self, tmp_path: Path):
        allow_file = tmp_path / "approvals.json"
        mgr = ApprovalManager(allow_file)
        assert mgr.is_allowed("anything") is False

    @pytest.mark.asyncio
    async def test_check_allowed_returns_true(self, tmp_path: Path):
        allow_file = tmp_path / "approvals.json"
        allow_file.write_text(json.dumps({"always_allow": ["gmail_send"]}))
        mgr = ApprovalManager(allow_file)
        tool = _make_tool("gmail_send")
        result = await mgr.check_and_approve(
            tool, {}, send_fn=AsyncMock(), receive_fn=AsyncMock()
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_check_user_says_yes(self, tmp_path: Path):
        allow_file = tmp_path / "approvals.json"
        mgr = ApprovalManager(allow_file)
        tool = _make_tool("gmail_send")
        result = await mgr.check_and_approve(
            tool, {"to": "a@b.com"},
            send_fn=AsyncMock(),
            receive_fn=AsyncMock(return_value="yes"),
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_check_user_says_no(self, tmp_path: Path):
        allow_file = tmp_path / "approvals.json"
        mgr = ApprovalManager(allow_file)
        tool = _make_tool("gmail_send")
        result = await mgr.check_and_approve(
            tool, {},
            send_fn=AsyncMock(),
            receive_fn=AsyncMock(return_value="no"),
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_check_user_says_always(self, tmp_path: Path):
        allow_file = tmp_path / "approvals.json"
        mgr = ApprovalManager(allow_file)
        tool = _make_tool("shell_exec")
        result = await mgr.check_and_approve(
            tool, {},
            send_fn=AsyncMock(),
            receive_fn=AsyncMock(return_value="always"),
        )
        assert result is True
        assert mgr.is_allowed("shell_exec") is True
        # Check persistence
        reloaded = ApprovalManager(allow_file)
        assert reloaded.is_allowed("shell_exec") is True
