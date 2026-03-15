from __future__ import annotations

import json
from pathlib import Path

import pytest

from synapse.models import SkillDefinition
from synapse.config.schema import AppConfig
from synapse.skill_runtime import (
    CommandExecutionResult,
    CommandRunner,
    DependencyInstallDisabledError,
    DockerExecutor,
    ResolvedSkillCommand,
    SkillEnvManager,
)
from synapse.skills import SkillRegistry


def _make_skill(tmp_path: Path, skill_id: str = "swing-trader", *, metadata: dict | None = None) -> SkillDefinition:
    skill_dir = tmp_path / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\n" + json.dumps(metadata or {}) + "\n---\n# Skill\n", encoding="utf-8")
    return SkillDefinition(
        skill_id=skill_id,
        name=skill_id,
        description="",
        instruction_markdown="# Skill",
        path=str(skill_dir / "SKILL.md"),
        metadata=metadata or {},
    )


def test_skill_env_manager_builds_venv_create_command(tmp_path: Path):
    manager = SkillEnvManager(tmp_path, python_executable="/usr/bin/python3")
    assert manager.build_create_command("scanner") == [
        "/usr/bin/python3",
        "-m",
        "venv",
        str(tmp_path / "var" / "skills" / "envs" / "scanner"),
    ]


@pytest.mark.asyncio
async def test_requirements_hash_change_triggers_install(tmp_path: Path):
    calls: list[tuple[list[str], Path | None]] = []

    async def runner(command, cwd):
        calls.append((list(command), cwd))

    skill = _make_skill(tmp_path)
    requirements = Path(skill.path).parent / "requirements.txt"
    requirements.write_text("httpx==0.27.0\n", encoding="utf-8")

    manager = SkillEnvManager(tmp_path, auto_install_enabled=True, run_command=runner)
    python_path = manager.python_path(skill.skill_id)
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    manager.state_path(skill.skill_id).write_text(
        json.dumps(
            {
                "hash": "old-hash",
                "installed_at": "2026-03-15T00:00:00Z",
                "python_path": str(python_path),
            }
        ),
        encoding="utf-8",
    )

    await manager.ensure_skill_env(skill)

    assert len(calls) == 1
    assert calls[0][0] == manager.build_install_command(skill.skill_id, requirements)
    state = json.loads(manager.state_path(skill.skill_id).read_text(encoding="utf-8"))
    assert state["hash"] != "old-hash"


@pytest.mark.asyncio
async def test_auto_install_disabled_raises_clear_error(tmp_path: Path):
    calls: list[list[str]] = []

    async def runner(command, cwd):
        calls.append(list(command))

    skill = _make_skill(tmp_path)
    requirements = Path(skill.path).parent / "requirements.txt"
    requirements.write_text("httpx==0.27.0\n", encoding="utf-8")

    manager = SkillEnvManager(tmp_path, auto_install_enabled=False, run_command=runner)
    python_path = manager.python_path(skill.skill_id)
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(DependencyInstallDisabledError) as excinfo:
        await manager.ensure_skill_env(skill)

    assert "SKILL_AUTO_INSTALL_DEPS=1" in str(excinfo.value)
    assert calls == []


@pytest.mark.parametrize(
    ("metadata", "allow_network_override", "expected_network"),
    [
        ({}, False, "none"),
        ({"network": True}, False, "bridge"),
        ({}, True, "bridge"),
    ],
)
def test_docker_command_construction_respects_network_and_mounts(
    tmp_path: Path,
    metadata: dict,
    allow_network_override: bool,
    expected_network: str,
):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    skill = _make_skill(workspace_root, metadata=metadata)
    skill_root = Path(skill.path).parent
    script_path = skill_root / "scripts" / "scanner.py"
    script_path.parent.mkdir()
    script_path.write_text("print('ok')\n", encoding="utf-8")
    data_path = workspace_root / "data.txt"
    data_path.write_text("payload\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    resolved = ResolvedSkillCommand(
        command=f"python3 {script_path} --input {data_path}",
        parts=("python3", str(script_path), "--input", str(data_path)),
        skill=skill,
        skill_root=skill_root,
    )
    executor = DockerExecutor(
        image="python:3.11-slim",
        allow_network_override=allow_network_override,
        mount_workspace=True,
    )

    command = executor.build_command(
        resolved,
        workspace_root=workspace_root,
        output_dir=output_dir,
    )

    assert command[:3] == ["docker", "run", "--rm"]
    assert command[command.index("--network") + 1] == expected_network
    assert f"{skill_root}:/skill:ro" in command
    assert f"{workspace_root}:/workspace:rw" in command
    assert "python" in command
    assert "/skill/scripts/scanner.py" in command
    assert "/workspace/data.txt" in command


def test_docker_command_omits_workspace_mount_when_skill_policy_disables_it(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    skill = _make_skill(workspace_root, metadata={"mount_workspace": False})
    skill_root = Path(skill.path).parent
    script_path = skill_root / "scripts" / "scanner.py"
    script_path.parent.mkdir()
    script_path.write_text("print('ok')\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    resolved = ResolvedSkillCommand(
        command=f"python3 {script_path}",
        parts=("python3", str(script_path)),
        skill=skill,
        skill_root=skill_root,
    )
    executor = DockerExecutor(mount_workspace=True)

    command = executor.build_command(
        resolved,
        workspace_root=workspace_root,
        output_dir=output_dir,
    )

    assert f"{workspace_root}:/workspace:rw" not in command


@pytest.mark.asyncio
async def test_command_runner_uses_skill_venv_python_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    skill_dir = tmp_path / "skills" / "swing-trader"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text(
        json.dumps({"id": "swing-trader", "name": "Swing Trader", "description": "scan"}),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("# Swing Trader\n", encoding="utf-8")
    registry = SkillRegistry(tmp_path / "skills")
    registry.load()
    skill = registry.get("swing-trader")
    script_path = skill_dir / "scripts" / "scanner.py"
    script_path.parent.mkdir()
    script_path.write_text("print('ok')\n", encoding="utf-8")

    manager = SkillEnvManager(tmp_path, auto_install_enabled=False)
    python_path = manager.python_path("swing-trader")
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    manager.state_path("swing-trader").write_text(
        json.dumps(
            {
                "hash": None,
                "installed_at": "2026-03-15T00:00:00Z",
                "python_path": str(python_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class _DockerSentinel:
        async def run(self, resolved, *, workspace_root):
            raise AssertionError("docker should not be used when isolation is disabled")

    captured: dict[str, object] = {}

    async def fake_run_process(command, *, cwd=None, timeout_seconds, max_output_bytes, mode):
        captured["command"] = list(command)
        captured["mode"] = mode
        return CommandExecutionResult(
            success=True,
            output="ok",
            error=None,
            exit_code=0,
            mode=mode,
        )

    monkeypatch.setattr("synapse.skill_runtime._run_process", fake_run_process)
    runner = CommandRunner(
        config=None,
        skill_registry=registry,
        env_manager=manager,
        docker_executor=_DockerSentinel(),  # type: ignore[arg-type]
    )

    result = await runner.run(f"python3 {script_path} scan", cwd=str(tmp_path))

    assert result.error is None
    assert captured["mode"] == "host"
    assert captured["command"][0] == str(python_path)
    assert captured["command"][1:] == [str(script_path), "scan"]


@pytest.mark.asyncio
async def test_command_runner_uses_docker_for_skill_when_isolation_enabled(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "swing-trader"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text(
        json.dumps({"id": "swing-trader", "name": "Swing Trader", "description": "scan"}),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("---\nnetwork: true\n---\n# Swing Trader\n", encoding="utf-8")
    registry = SkillRegistry(tmp_path / "skills")
    registry.load()
    script_path = skill_dir / "scripts" / "scanner.py"
    script_path.parent.mkdir()
    script_path.write_text("print('ok')\n", encoding="utf-8")

    config = AppConfig.from_root(tmp_path)
    config.execution.isolated_execution_enabled = True
    config.execution.docker_allow_network = False
    config.execution.docker_mount_workspace = True

    class _DockerProbe:
        def __init__(self) -> None:
            self.calls: list[tuple[ResolvedSkillCommand, Path | None]] = []

        async def run(self, resolved, *, workspace_root):
            self.calls.append((resolved, workspace_root))
            return CommandExecutionResult(
                success=True,
                output="docker ok",
                error=None,
                exit_code=0,
                mode="docker",
                artifacts={"network_enabled": True},
            )

    docker = _DockerProbe()
    runner = CommandRunner(
        config=config,
        skill_registry=registry,
        docker_executor=docker,  # type: ignore[arg-type]
    )

    result = await runner.run(f"python3 {script_path} scan", cwd=str(tmp_path))

    assert result.error is None
    assert result.mode == "docker"
    assert result.output == "docker ok"
    assert len(docker.calls) == 1
    assert docker.calls[0][0].skill is not None
    assert docker.calls[0][1] == tmp_path
