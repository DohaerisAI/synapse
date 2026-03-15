"""Tests for builtin tools — RED → GREEN."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synapse.config.schema import AppConfig
from synapse.jobs import JobService
from synapse.store import SQLiteStore
from synapse.skill_runtime import CommandExecutionResult
from synapse.skills import SkillRegistry
from synapse.tools.registry import ToolContext, ToolRegistry, ToolResult


def _make_ctx(*, root: Path | None = None, **overrides) -> ToolContext:
    memory = MagicMock()
    store = MagicMock()
    workspace_root = root or Path(tempfile.mkdtemp(prefix="synapse-tool-tests-"))
    workspace_root.mkdir(parents=True, exist_ok=True)
    config = AppConfig.from_root(workspace_root)
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
        assert registry.get("shell_readonly") is not None

    def test_repo_tools_exist(self, registry: ToolRegistry):
        for name in ("repo_status", "repo_diffstat", "repo_diff", "repo_grep", "repo_open"):
            assert registry.get(name) is not None, f"missing tool: {name}"

    def test_swing_tools_exist(self, registry: ToolRegistry):
        assert registry.get("swing_analyze") is not None
        assert registry.get("swing_scan") is not None

    @pytest.mark.asyncio
    async def test_shell_readonly_blocks_unsafe(self, registry: ToolRegistry, tmp_path: Path):
        ctx = _make_ctx(root=tmp_path)
        tool = registry.get("shell_readonly")

        assert tool is not None

        blocked = await tool.execute({"command": "rm -rf /"}, ctx=ctx)
        assert blocked.error is not None
        assert "only allows" in blocked.error

    @pytest.mark.asyncio
    async def test_shell_readonly_allows_git_status(self, registry: ToolRegistry, tmp_path: Path):
        runner = MagicMock()
        runner.run = AsyncMock(
            return_value=CommandExecutionResult(
                success=True,
                output="## main...origin/main\n",
                error=None,
                exit_code=0,
                mode="host",
            )
        )
        ctx = _make_ctx(root=tmp_path, command_runner=runner)
        tool = registry.get("shell_readonly")
        result = await tool.execute({"command": "git status -sb", "cwd": "."}, ctx=ctx)
        assert result.error is None
        assert "main" in result.output
        runner.run.assert_awaited_once_with("git status -sb", cwd=str(tmp_path))

    def test_load_skill_exists(self, registry: ToolRegistry):
        assert registry.get("load_skill") is not None

    def test_reminder_create_exists(self, registry: ToolRegistry):
        assert registry.get("reminder_create") is not None

    def test_filesystem_tools_exist(self, registry: ToolRegistry):
        for name in ("fs_read", "fs_write", "fs_edit", "patch_apply"):
            assert registry.get(name) is not None, f"missing tool: {name}"


class TestSwingTools:
    @pytest.mark.asyncio
    async def test_swing_analyze_uses_runner_argv_and_returns_parsed_json(self, registry: ToolRegistry, tmp_path: Path):
        skill_dir = tmp_path / "skills" / "swing-trader"
        skill_dir.mkdir(parents=True)
        (skill_dir / "manifest.json").write_text(
            json.dumps({"id": "swing-trader", "name": "Swing Trader", "description": "scan"}),
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text("# Swing Trader\n", encoding="utf-8")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "scanner.py").write_text("print('ok')\n", encoding="utf-8")
        skill_registry = SkillRegistry(tmp_path / "skills")
        skill_registry.load()

        runner = MagicMock()
        runner.run_argv = AsyncMock(
            return_value=CommandExecutionResult(
                success=True,
                output='{"symbol":"LAURUSLABS","rsi":61.5}',
                error=None,
                exit_code=0,
                mode="host",
                artifacts={},
            )
        )
        ctx = _make_ctx(root=tmp_path, command_runner=runner, skill_registry=skill_registry)

        result = await registry.get("swing_analyze").execute(
            {"symbol": "laurus labs", "timeframe": "daily"},
            ctx=ctx,
        )

        assert result.error is None
        runner.run_argv.assert_awaited_once()
        argv = runner.run_argv.await_args.args[0]
        assert argv[:3] == ["python3", str(skill_dir / "scripts" / "scanner.py"), "analyze"]
        assert "--symbol" in argv
        assert argv[argv.index("--symbol") + 1] == "LAURUSLABS"
        payload = json.loads(result.output)
        assert payload["parsed"]["symbol"] == "LAURUSLABS"
        assert "raw" in payload

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

    @pytest.mark.asyncio
    async def test_shell_exec_uses_command_runner_when_available(self, registry: ToolRegistry):
        runner = MagicMock()
        runner.run = AsyncMock(
            return_value=CommandExecutionResult(
                success=True,
                output="ok",
                error=None,
                exit_code=0,
                mode="docker",
                artifacts={"skill_id": "swing-trader"},
            )
        )
        ctx = _make_ctx(command_runner=runner)
        tool = registry.get("shell_exec")
        result = await tool.execute({"command": "python3 /tmp/skill/scripts/run.py", "cwd": "/tmp"}, ctx=ctx)
        assert result.error is None
        assert result.output == "ok"
        assert result.artifacts["mode"] == "docker"
        assert result.artifacts["cwd"] == "/tmp"
        runner.run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shell_exec_background_returns_job_id(self, registry: ToolRegistry, tmp_path: Path):
        runner = MagicMock()
        runner.run = AsyncMock()
        store = SQLiteStore(tmp_path / "var" / "runtime.sqlite3")
        store.initialize()
        service = JobService(
            store=store,
            root=tmp_path / "var" / "jobs",
            execute_job=AsyncMock(return_value=ToolResult(output="ok")),
        )
        ctx = _make_ctx(root=tmp_path, store=store, command_runner=runner, job_service=service, run_id="run-1")

        result = await registry.get("shell_exec").execute({"command": "pwd", "background": True}, ctx=ctx)

        assert result.error is None
        assert result.artifacts["job_id"]
        runner.run.assert_not_called()

    def test_no_approval_for_read_only_git_inspection(self, registry: ToolRegistry):
        tool = registry.get("shell_exec")
        assert tool.check_approval({"command": "git status -sb"}) is False
        assert tool.check_approval({"command": "git diff --stat"}) is False

    def test_no_approval_for_read_only_fs_commands(self, registry: ToolRegistry):
        tool = registry.get("shell_exec")
        assert tool.check_approval({"command": "cat README.md"}) is False
        assert tool.check_approval({"command": "head -n 20 README.md"}) is False
        assert tool.check_approval({"command": "tail -n 20 README.md"}) is False
        assert tool.check_approval({"command": "sed -n '1,5p' README.md"}) is False
        # sed without -n could be used for scripting; keep it gated
        assert tool.check_approval({"command": "sed 's/x/y/' README.md"}) is True

    def test_no_approval_for_swing_trader_analyze_and_scan(self, registry: ToolRegistry):
        tool = registry.get("shell_exec")
        assert tool.check_approval({
            "command": "python3 skills/swing-trader/scripts/scanner.py analyze --symbol LAURUSLABS --timeframe daily"
        }) is False
        assert tool.check_approval({
            "command": "python3 skills/swing-trader/scripts/scanner.py scan --pattern all --watchlist nifty50"
        }) is False
        assert tool.check_approval({"command": "git diff"}) is False


class TestRepoTools:
    @pytest.mark.asyncio
    async def test_repo_status_uses_git_status_short_branch(self, registry: ToolRegistry):
        runner = MagicMock()
        runner.run = AsyncMock(
            return_value=CommandExecutionResult(
                success=True,
                output="## main",
                error=None,
                exit_code=0,
                mode="host",
            )
        )
        ctx = _make_ctx(command_runner=runner)

        result = await registry.get("repo_status").execute({"cwd": "/repo"}, ctx=ctx)

        assert result.error is None
        assert result.output == "## main"
        runner.run.assert_awaited_once_with("git status -sb", cwd="/repo")

    @pytest.mark.asyncio
    async def test_repo_diffstat_uses_git_diff_stat(self, registry: ToolRegistry):
        runner = MagicMock()
        runner.run = AsyncMock(
            return_value=CommandExecutionResult(
                success=True,
                output=" file.py | 2 +-",
                error=None,
                exit_code=0,
                mode="host",
            )
        )
        ctx = _make_ctx(command_runner=runner)

        result = await registry.get("repo_diffstat").execute({"cwd": "/repo"}, ctx=ctx)

        assert result.error is None
        assert "file.py" in result.output
        runner.run.assert_awaited_once_with("git diff --stat", cwd="/repo")

    @pytest.mark.asyncio
    async def test_repo_diff_uses_git_diff(self, registry: ToolRegistry):
        runner = MagicMock()
        runner.run = AsyncMock(
            return_value=CommandExecutionResult(
                success=True,
                output="diff --git a/file.py b/file.py",
                error=None,
                exit_code=0,
                mode="host",
            )
        )
        ctx = _make_ctx(command_runner=runner)

        result = await registry.get("repo_diff").execute({"cwd": "/repo"}, ctx=ctx)

        assert result.error is None
        assert "diff --git" in result.output
        runner.run.assert_awaited_once_with("git diff", cwd="/repo")

    @pytest.mark.asyncio
    async def test_repo_grep_calls_runner(self, registry: ToolRegistry, tmp_path: Path):
        runner = MagicMock()
        runner.run_argv = AsyncMock(
            return_value=CommandExecutionResult(
                success=True,
                output="src/app.py:12:needle",
                error=None,
                exit_code=0,
                mode="host",
            )
        )
        ctx = _make_ctx(root=tmp_path, command_runner=runner)

        with patch("synapse.tools.builtins.shutil.which", return_value="/usr/bin/rg"):
            result = await registry.get("repo_grep").execute(
                {"query": "needle", "cwd": ".", "glob": "*.py", "max": 5},
                ctx=ctx,
            )

        assert result.error is None
        assert "needle" in result.output
        runner.run_argv.assert_awaited_once_with(
            ["rg", "-n", "--color", "never", "--glob", "*.py", "--max-count", "5", "needle", "."],
            cwd=str(tmp_path),
        )

    @pytest.mark.asyncio
    async def test_repo_open_reads_from_repo_root(self, registry: ToolRegistry, tmp_path: Path):
        (tmp_path / "README.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
        ctx = _make_ctx(root=tmp_path)

        result = await registry.get("repo_open").execute({"path": "README.md", "from": 2, "lines": 2}, ctx=ctx)

        assert result.error is None
        assert result.output == "two\nthree"
        assert result.artifacts["line_range"] == {"from": 2, "to": 3}


class TestFilesystemTools:
    def test_fs_write_approval_only_for_safe_paths(self, tmp_path: Path):
        from synapse.tools.builtins import register_builtin_tools

        config = AppConfig.from_root(tmp_path)
        registry = ToolRegistry()
        register_builtin_tools(registry, config=config)

        write_tool = registry.get("fs_write")
        edit_tool = registry.get("fs_edit")

        assert write_tool.check_approval({"path": "skills/demo.txt"}) is False
        assert edit_tool.check_approval({"path": "var/proposals/demo/PATCH.diff"}) is False
        assert write_tool.check_approval({"path": "README.md"}) is True

        config.filesystem.require_approval = True
        registry = ToolRegistry()
        register_builtin_tools(registry, config=config)
        assert registry.get("fs_write").check_approval({"path": "skills/demo.txt"}) is True

    @pytest.mark.asyncio
    async def test_fs_read_slices_correctly_and_blocks_traversal(self, registry: ToolRegistry, tmp_path: Path):
        (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
        ctx = _make_ctx(root=tmp_path)

        result = await registry.get("fs_read").execute({"path": "notes.txt", "from": 2, "lines": 2}, ctx=ctx)

        assert result.error is None
        assert result.output == "beta\ngamma"
        assert result.artifacts["line_range"] == {"from": 2, "to": 3}

        blocked = await registry.get("fs_read").execute({"path": "../outside.txt"}, ctx=ctx)

        assert blocked.error is not None
        assert "outside the workspace root" in blocked.error

    @pytest.mark.asyncio
    async def test_fs_write_creates_parent_dirs_with_mkdirp(self, registry: ToolRegistry, tmp_path: Path):
        ctx = _make_ctx(root=tmp_path)

        result = await registry.get("fs_write").execute(
            {"path": "skills/demo/note.txt", "content": "hello\nworld\n", "mkdirp": True},
            ctx=ctx,
        )

        target = tmp_path / "skills" / "demo" / "note.txt"
        assert result.error is None
        assert target.read_text(encoding="utf-8") == "hello\nworld\n"
        assert result.artifacts["bytes_written"] == len("hello\nworld\n".encode("utf-8"))
        assert result.artifacts["changed"] is True

    @pytest.mark.asyncio
    async def test_fs_edit_replaces_exact_match_only(self, registry: ToolRegistry, tmp_path: Path):
        target = tmp_path / "skills" / "demo.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("hello world\nnext\n", encoding="utf-8")
        ctx = _make_ctx(root=tmp_path)

        result = await registry.get("fs_edit").execute(
            {"path": "skills/demo.txt", "old": "hello world", "new": "hi world"},
            ctx=ctx,
        )

        assert result.error is None
        assert target.read_text(encoding="utf-8") == "hi world\nnext\n"
        assert result.artifacts["changed"] is True
        assert result.artifacts["line_range"] == {"from": 1, "to": 1}

        target.write_text("repeat\nrepeat\n", encoding="utf-8")
        failed = await registry.get("fs_edit").execute(
            {"path": "skills/demo.txt", "old": "repeat", "new": "done"},
            ctx=ctx,
        )

        assert failed.error is not None
        assert "exactly once" in failed.error
        assert target.read_text(encoding="utf-8") == "repeat\nrepeat\n"

    @pytest.mark.asyncio
    async def test_patch_apply_writes_manual_proposal_and_calls_git_apply(
        self,
        registry: ToolRegistry,
        tmp_path: Path,
    ):
        runner = MagicMock()
        runner.run_argv = AsyncMock(
            return_value=CommandExecutionResult(
                success=True,
                output="applied",
                error=None,
                exit_code=0,
                mode="host",
            )
        )
        ctx = _make_ctx(root=tmp_path, command_runner=runner)
        patch_text = (
            "diff --git a/demo.txt b/demo.txt\n"
            "--- a/demo.txt\n"
            "+++ b/demo.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        tool = registry.get("patch_apply")
        assert tool.check_approval({"patch": patch_text}) is True

        result = await tool.execute({"patch": patch_text, "cwd": "."}, ctx=ctx)

        assert result.error is None
        assert result.artifacts["applied"] is True

        manual_root = tmp_path / "var" / "proposals" / "manual"
        proposal_dirs = [item for item in manual_root.iterdir() if item.is_dir()]
        assert len(proposal_dirs) == 1

        patch_path = proposal_dirs[0] / "PATCH.diff"
        metadata_path = proposal_dirs[0] / "METADATA.json"
        assert patch_path.read_text(encoding="utf-8") == patch_text
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["tool"] == "patch_apply"
        assert metadata["cwd"] == str(tmp_path)

        runner.run_argv.assert_awaited_once_with(["git", "apply", str(patch_path)], cwd=str(tmp_path))


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
