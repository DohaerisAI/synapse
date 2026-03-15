from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time
from unittest.mock import AsyncMock

from synapse.approvals import ApprovalManager
from synapse.config.schema import AppConfig
from synapse.jobs import JobService
from synapse.models import DeliveryTarget, JobStatus
from synapse.skill_runtime import CommandExecutionResult
from synapse.store import SQLiteStore
from synapse.tools.builtins import register_builtin_tools
from synapse.tools.registry import ToolContext, ToolRegistry, ToolResult


def _make_ctx(tmp_path: Path, *, runner=None, store: SQLiteStore | None = None, job_service=None) -> ToolContext:
    config = AppConfig.from_root(tmp_path)
    config.provider.codex_model = "gpt-5.4"
    config.provider.codex_auth_file = str(tmp_path / "auth.json")
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    memory = AsyncMock()
    active_store = store or SQLiteStore(config.paths.sqlite_path)
    active_store.initialize()
    return ToolContext(
        session_key="sess-1",
        user_id="user-1",
        memory=memory,
        store=active_store,
        config=config,
        run_id="run-1",
        delivery_target=DeliveryTarget(adapter="telegram", channel_id="22", user_id="user-1"),
        job_service=job_service,
        command_runner=runner,
    )


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    return registry


def _write_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("print('old')\n", encoding="utf-8")
    return repo


async def test_codex_propose_writes_bundle_under_var_proposals(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    runner = AsyncMock()

    async def fake_run_argv(argv, *, cwd=None, env=None):
        output_path = Path(argv[argv.index("-o") + 1])
        repo_path = Path(argv[argv.index("-C") + 1])
        (repo_path / "app.py").write_text("print('new')\n", encoding="utf-8")
        output_path.write_text(
            json.dumps(
                {
                    "plan": ["Update app.py output."],
                    "summary": "Updated `app.py` output in proposal form.",
                    "tests": ["python -m py_compile app.py"],
                }
            ),
            encoding="utf-8",
        )
        return CommandExecutionResult(
            success=True,
            output="ok",
            error=None,
            exit_code=0,
            mode="host",
        )

    runner.run_argv.side_effect = fake_run_argv
    ctx = _make_ctx(tmp_path, runner=runner)
    registry = _make_registry()

    result = await registry.get("codex_propose").execute(
        {
            "repo_path": str(repo),
            "task": "Change app.py to print new",
            "files": ["app.py"],
        },
        ctx=ctx,
    )

    assert result.error is None
    proposal_id = result.artifacts["proposal_id"]
    proposal_root = tmp_path / "var" / "proposals" / proposal_id
    assert (proposal_root / "PLAN.md").exists()
    assert (proposal_root / "PATCH.diff").exists()
    assert (proposal_root / "TESTS.md").exists()
    assert (proposal_root / "SUMMARY.md").exists()
    assert "diff --git a/app.py b/app.py" in (proposal_root / "PATCH.diff").read_text(encoding="utf-8")
    record = ctx.store.get_codex_proposal(proposal_id)
    assert record is not None
    assert record.repo_path == str(repo)
    assert record.test_commands == ["python -m py_compile app.py"]


async def test_codex_apply_proposal_requires_approval_then_applies(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    runner = AsyncMock()
    runner.run_argv = AsyncMock(
        return_value=CommandExecutionResult(
            success=True,
            output="applied",
            error=None,
            exit_code=0,
            mode="host",
        )
    )
    store = SQLiteStore(tmp_path / "var" / "runtime.sqlite3")
    store.initialize()
    ctx = _make_ctx(tmp_path, runner=runner, store=store)
    registry = _make_registry()

    proposal_id = "proposal-apply"
    proposal_root = tmp_path / "var" / "proposals" / proposal_id
    proposal_root.mkdir(parents=True, exist_ok=True)
    patch_path = proposal_root / "PATCH.diff"
    patch_path.write_text(
        "\n".join(
            [
                "diff --git a/app.py b/app.py",
                "--- a/app.py",
                "+++ b/app.py",
                "@@ -1 +1 @@",
                "-print('old')",
                "+print('new')",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (proposal_root / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (proposal_root / "TESTS.md").write_text("# Tests\n", encoding="utf-8")
    (proposal_root / "SUMMARY.md").write_text("Summary\n", encoding="utf-8")
    store.create_codex_proposal(
        proposal_id=proposal_id,
        repo_path=str(repo),
        proposal_path=str(proposal_root),
        task="apply test",
        context="",
        files=["app.py"],
        test_commands=["python -m py_compile app.py"],
        summary="summary",
        status="PROPOSED",
    )

    tool = registry.get("codex_apply_proposal")
    assert tool.check_approval({"proposal_id": proposal_id}) is True

    approvals = ApprovalManager(tmp_path / "approvals.json")
    blocked = await approvals.check_and_approve(tool, {"proposal_id": proposal_id})
    assert blocked is False

    approved = await approvals.check_and_approve(
        tool,
        {"proposal_id": proposal_id},
        send_fn=AsyncMock(),
        receive_fn=AsyncMock(return_value="yes"),
    )
    assert approved is True

    result = await tool.execute({"proposal_id": proposal_id}, ctx=ctx)

    assert result.error is None
    runner.run_argv.assert_awaited_once_with(
        ["git", "apply", str(patch_path)],
        cwd=str(repo),
        env=None,
    )
    assert ctx.store.get_codex_proposal(proposal_id).status == "APPLIED"


async def test_codex_run_tests_executes_stored_commands_via_runner(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    runner = AsyncMock()
    runner.run_argv = AsyncMock(
        return_value=CommandExecutionResult(
            success=True,
            output="patched",
            error=None,
            exit_code=0,
            mode="host",
        )
    )
    runner.run = AsyncMock(
        side_effect=[
            CommandExecutionResult(success=True, output="ok", error=None, exit_code=0, mode="host"),
            CommandExecutionResult(success=True, output="ok", error=None, exit_code=0, mode="host"),
        ]
    )
    store = SQLiteStore(tmp_path / "var" / "runtime.sqlite3")
    store.initialize()
    ctx = _make_ctx(tmp_path, runner=runner, store=store)
    registry = _make_registry()

    proposal_id = "proposal-tests"
    proposal_root = tmp_path / "var" / "proposals" / proposal_id
    proposal_root.mkdir(parents=True, exist_ok=True)
    patch_path = proposal_root / "PATCH.diff"
    patch_path.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
    (proposal_root / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (proposal_root / "TESTS.md").write_text("# Tests\n", encoding="utf-8")
    (proposal_root / "SUMMARY.md").write_text("Summary\n", encoding="utf-8")
    store.create_codex_proposal(
        proposal_id=proposal_id,
        repo_path=str(repo),
        proposal_path=str(proposal_root),
        task="run tests",
        context="",
        files=["app.py"],
        test_commands=["python -m py_compile app.py", "pytest -q tests/test_codex_tools.py"],
        summary="summary",
        status="PROPOSED",
    )

    result = await registry.get("codex_run_tests").execute({"proposal_id": proposal_id}, ctx=ctx)

    assert result.error is None
    assert "PASS: python -m py_compile app.py" in result.output
    assert "PASS: pytest -q tests/test_codex_tools.py" in result.output
    runner.run_argv.assert_awaited_once_with(
        ["git", "apply", str(patch_path)],
        cwd=runner.run.await_args_list[0].kwargs["cwd"],
        env=None,
    )
    assert [call.args[0] for call in runner.run.await_args_list] == [
        "python -m py_compile app.py",
        "pytest -q tests/test_codex_tools.py",
    ]
    for call in runner.run.await_args_list:
        assert call.kwargs["cwd"] != str(repo)
    assert (proposal_root / "TEST_RESULTS.md").exists()


async def test_codex_propose_background_returns_job_id_quickly(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "var" / "runtime.sqlite3")
    store.initialize()

    async def fake_execute(job, artifacts, cancel_event):
        await asyncio.sleep(0.2)
        return ToolResult(output="proposal ready")

    service = JobService(
        store=store,
        root=tmp_path / "var" / "jobs",
        execute_job=fake_execute,
    )
    ctx = _make_ctx(tmp_path, store=store, job_service=service)
    registry = _make_registry()
    repo = _write_repo(tmp_path)

    started = time.perf_counter()
    result = await registry.get("codex_propose").execute(
        {
            "repo_path": str(repo),
            "task": "Generate a proposal",
            "background": True,
        },
        ctx=ctx,
    )
    elapsed = time.perf_counter() - started

    assert result.error is None
    assert elapsed < 0.2
    assert result.artifacts["job_id"]
    assert store.get_job(result.artifacts["job_id"]).status == JobStatus.QUEUED


async def test_codex_run_tests_background_returns_job_id(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "var" / "runtime.sqlite3")
    store.initialize()
    service = JobService(
        store=store,
        root=tmp_path / "var" / "jobs",
        execute_job=AsyncMock(return_value=ToolResult(output="ok")),
    )
    ctx = _make_ctx(tmp_path, store=store, job_service=service)
    registry = _make_registry()

    result = await registry.get("codex_run_tests").execute(
        {"proposal_id": "proposal-tests", "background": True},
        ctx=ctx,
    )

    assert result.error is None
    assert result.artifacts["job_id"]
    assert store.get_job(result.artifacts["job_id"]).tool_name == "codex_run_tests"


async def test_job_service_executes_job_and_triggers_follow_up_callback(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "var" / "runtime.sqlite3")
    store.initialize()
    delivered = threading.Event()
    deliveries: list[tuple[str, str]] = []

    async def fake_execute(job, artifacts, cancel_event):
        return ToolResult(output="proposal completed", artifacts={"proposal_id": "p-1"})

    def on_terminal(job, result, artifacts) -> None:
        deliveries.append((job.job_id, job.status.value))
        delivered.set()

    service = JobService(
        store=store,
        root=tmp_path / "var" / "jobs",
        execute_job=fake_execute,
        on_terminal=on_terminal,
    )
    job = service.enqueue_job(
        tool_name="codex_propose",
        params={"repo_path": str(tmp_path / "repo"), "task": "Generate a proposal"},
        session_key="sess-1",
        delivery_target=DeliveryTarget(adapter="telegram", channel_id="22", user_id="user-1"),
    )

    service.start()
    try:
        assert delivered.wait(3.0)
    finally:
        service.stop()

    stored = store.get_job(job.job_id)
    assert stored is not None
    assert stored.status == JobStatus.SUCCEEDED
    assert deliveries == [(job.job_id, JobStatus.SUCCEEDED.value)]
