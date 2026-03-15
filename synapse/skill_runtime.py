from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from .config.schema import AppConfig
from .models import SkillDefinition
from .skills import SkillRegistry

_MAX_CHUNK_BYTES = 4096
_OUTPUT_DIR_ENV = "SYNAPSE_OUTPUT_DIR"
_NETWORK_METADATA_KEY = "network"
_MOUNT_WORKSPACE_METADATA_KEY = "mount_workspace"
_AUTO_INSTALL_FLAG = "SKILL_AUTO_INSTALL_DEPS"


class DependencyInstallDisabledError(RuntimeError):
    """Raised when a skill needs pip install but auto-install is disabled."""


@dataclass(frozen=True, slots=True)
class SkillEnvState:
    requirements_hash: str | None
    installed_at: str
    python_path: str


@dataclass(frozen=True, slots=True)
class ResolvedSkillCommand:
    command: str
    parts: tuple[str, ...]
    skill: SkillDefinition | None = None
    skill_root: Path | None = None


@dataclass(slots=True)
class CommandExecutionResult:
    success: bool
    output: str
    error: str | None
    exit_code: int
    mode: str
    artifacts: dict[str, object] = field(default_factory=dict)


RunCommand = Callable[[Sequence[str], Path | None], Awaitable[None]]


class SkillEnvManager:
    STATE_FILENAME = "env_state.json"

    def __init__(
        self,
        root: Path,
        *,
        python_executable: str | None = None,
        auto_install_enabled: bool = False,
        run_command: RunCommand | None = None,
    ) -> None:
        self.root = root
        self.python_executable = python_executable or sys.executable
        self.auto_install_enabled = auto_install_enabled
        self._run_command = run_command or self._default_run_command

    def env_dir(self, skill_id: str) -> Path:
        return self.root / "var" / "skills" / "envs" / skill_id

    def state_path(self, skill_id: str) -> Path:
        return self.env_dir(skill_id) / self.STATE_FILENAME

    def python_path(self, skill_id: str) -> Path:
        env_dir = self.env_dir(skill_id)
        if os.name == "nt":
            return env_dir / "Scripts" / "python.exe"
        return env_dir / "bin" / "python"

    def build_create_command(self, skill_id: str) -> list[str]:
        return [self.python_executable, "-m", "venv", str(self.env_dir(skill_id))]

    def build_install_command(self, skill_id: str, requirements_path: Path) -> list[str]:
        return [
            str(self.python_path(skill_id)),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirements_path),
        ]

    async def ensure_skill_env(self, skill: SkillDefinition) -> SkillEnvState:
        skill_root = self._skill_root(skill)
        requirements_path = skill_root / "requirements.txt"
        requirements_hash = self._requirements_hash(requirements_path)
        env_dir = self.env_dir(skill.skill_id)
        python_path = self.python_path(skill.skill_id)
        env_dir.parent.mkdir(parents=True, exist_ok=True)

        created = False
        if not python_path.exists():
            await self._run_command(self.build_create_command(skill.skill_id), skill_root)
            created = True

        state = self._read_state(skill.skill_id)
        install_needed = requirements_hash is not None and (
            created
            or state is None
            or state.requirements_hash != requirements_hash
        )
        if install_needed:
            if not self.auto_install_enabled:
                raise DependencyInstallDisabledError(
                    f"skill '{skill.skill_id}' needs dependency install from {requirements_path}, "
                    f"but auto-install is disabled. Set {_AUTO_INSTALL_FLAG}=1 to allow pip install."
                )
            await self._run_command(self.build_install_command(skill.skill_id, requirements_path), skill_root)

        if (
            created
            or install_needed
            or state is None
            or state.requirements_hash != requirements_hash
            or state.python_path != str(python_path)
        ):
            state = SkillEnvState(
                requirements_hash=requirements_hash,
                installed_at=_utc_now(),
                python_path=str(python_path),
            )
            self._write_state(skill.skill_id, state)
        return state

    def _skill_root(self, skill: SkillDefinition) -> Path:
        if not skill.path:
            raise ValueError(f"skill '{skill.skill_id}' does not expose a path")
        return Path(skill.path).resolve(strict=False).parent

    def _requirements_hash(self, requirements_path: Path) -> str | None:
        if not requirements_path.exists():
            return None
        digest = hashlib.sha256()
        digest.update(requirements_path.read_bytes())
        return digest.hexdigest()

    def _read_state(self, skill_id: str) -> SkillEnvState | None:
        state_path = self.state_path(skill_id)
        if not state_path.exists():
            return None
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return SkillEnvState(
            requirements_hash=payload.get("hash"),
            installed_at=str(payload.get("installed_at", "")),
            python_path=str(payload.get("python_path", "")),
        )

    def _write_state(self, skill_id: str, state: SkillEnvState) -> None:
        state_path = self.state_path(skill_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "hash": state.requirements_hash,
                    "installed_at": state.installed_at,
                    "python_path": state.python_path,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    async def _default_run_command(self, command: Sequence[str], cwd: Path | None) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            return
        detail = (stderr or stdout or b"").decode(errors="replace").strip() or f"exit code {process.returncode}"
        raise RuntimeError(detail)


class DockerExecutor:
    SKILL_MOUNT = Path("/skill")
    WORKSPACE_MOUNT = Path("/workspace")
    OUTPUT_MOUNT = Path("/output")

    def __init__(
        self,
        *,
        image: str = "python:3.11-slim",
        allow_network_override: bool = False,
        mount_workspace: bool = True,
        timeout_seconds: int = 60,
        max_output_bytes: int = 64 * 1024,
    ) -> None:
        self.image = image
        self.allow_network_override = allow_network_override
        self.mount_workspace = mount_workspace
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def build_command(
        self,
        resolved: ResolvedSkillCommand,
        *,
        workspace_root: Path | None,
        output_dir: Path,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        if resolved.skill_root is None:
            raise ValueError("docker execution requires a resolved skill root")

        network_enabled = self._network_enabled(resolved.skill)
        parts = self._rewrite_parts_for_container(
            resolved.parts,
            resolved.skill_root,
            workspace_root,
            resolved.skill,
        )
        docker_command = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--workdir",
            str(self.SKILL_MOUNT),
            "--network",
            "bridge" if network_enabled else "none",
            "-v",
            f"{resolved.skill_root}:{self.SKILL_MOUNT}:ro",
        ]
        if workspace_root is not None and self._workspace_mount_enabled(resolved.skill):
            docker_command.extend(["-v", f"{workspace_root}:{self.WORKSPACE_MOUNT}:rw"])
        docker_command.extend(
            [
                "-v",
                f"{output_dir}:{self.OUTPUT_MOUNT}:rw",
                "-e",
                "PYTHONUNBUFFERED=1",
                "-e",
                f"{_OUTPUT_DIR_ENV}={self.OUTPUT_MOUNT}",
            ]
        )
        if env:
            for key, value in sorted(env.items()):
                docker_command.extend(["-e", f"{key}={value}"])
        docker_command.append(self.image)
        docker_command.extend(parts)
        return docker_command

    async def run(
        self,
        resolved: ResolvedSkillCommand,
        *,
        workspace_root: Path | None,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
    ) -> CommandExecutionResult:
        with tempfile.TemporaryDirectory(prefix="synapse-skill-output-") as temp_dir:
            command = self.build_command(
                resolved,
                workspace_root=workspace_root,
                output_dir=Path(temp_dir),
                env=env,
            )
            result = await _run_process(
                command,
                timeout_seconds=self.timeout_seconds,
                max_output_bytes=self.max_output_bytes,
                mode="docker",
                cancel_event=cancel_event,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        artifacts = dict(result.artifacts)
        artifacts.update(
            {
                "image": self.image,
                "network_enabled": self._network_enabled(resolved.skill),
                "workspace_mounted": self._workspace_mount_enabled(resolved.skill),
            }
        )
        result.artifacts = artifacts
        return result

    def _rewrite_parts_for_container(
        self,
        parts: Sequence[str],
        skill_root: Path,
        workspace_root: Path | None,
        skill: SkillDefinition | None,
    ) -> list[str]:
        rewritten = list(parts)
        if rewritten and _is_python_command(rewritten[0]):
            rewritten[0] = "python"
        return [
            _rewrite_path_token_for_container(
                token,
                skill_root=skill_root,
                workspace_root=workspace_root if self._workspace_mount_enabled(skill) else None,
                skill_mount=self.SKILL_MOUNT,
                workspace_mount=self.WORKSPACE_MOUNT,
            )
            for token in rewritten
        ]

    def _network_enabled(self, skill: SkillDefinition | None) -> bool:
        if self.allow_network_override:
            return True
        if skill is None:
            return False
        return bool(skill.metadata.get(_NETWORK_METADATA_KEY))

    def _workspace_mount_enabled(self, skill: SkillDefinition | None) -> bool:
        if not self.mount_workspace:
            return False
        if skill is None:
            return True
        if _MOUNT_WORKSPACE_METADATA_KEY not in skill.metadata:
            return True
        return bool(skill.metadata.get(_MOUNT_WORKSPACE_METADATA_KEY))


class CommandRunner:
    def __init__(
        self,
        *,
        config: AppConfig | None,
        skill_registry: SkillRegistry | None,
        env_manager: SkillEnvManager | None = None,
        docker_executor: DockerExecutor | None = None,
    ) -> None:
        self.config = config
        self.skill_registry = skill_registry
        workspace_root = config.paths.root if config is not None else Path.cwd()
        execution = getattr(config, "execution", None)
        self.env_manager = env_manager or SkillEnvManager(
            workspace_root,
            auto_install_enabled=bool(getattr(execution, "skill_auto_install_deps", False)),
        )
        self.docker_executor = docker_executor or DockerExecutor(
            image=str(getattr(execution, "docker_image", "python:3.11-slim")),
            allow_network_override=bool(getattr(execution, "docker_allow_network", False)),
            mount_workspace=bool(getattr(execution, "docker_mount_workspace", True)),
            timeout_seconds=int(getattr(execution, "timeout_seconds", 60)),
            max_output_bytes=int(getattr(execution, "max_output_bytes", 64 * 1024)),
        )
        self.timeout_seconds = int(getattr(execution, "timeout_seconds", 60))
        self.max_output_bytes = int(getattr(execution, "max_output_bytes", 64 * 1024))
        self.isolated_enabled = bool(getattr(execution, "isolated_execution_enabled", False))

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
    ) -> CommandExecutionResult:
        resolved = self.resolve(command, cwd=cwd)
        return await self._run_resolved(
            resolved,
            cwd=cwd,
            env=env,
            cancel_event=cancel_event,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    async def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
    ) -> CommandExecutionResult:
        command = " ".join(shlex.quote(part) for part in argv)
        resolved = self.resolve(command, cwd=cwd)
        if tuple(argv) != resolved.parts:
            resolved = ResolvedSkillCommand(
                command=command,
                parts=tuple(argv),
                skill=resolved.skill,
                skill_root=resolved.skill_root,
            )
        return await self._run_resolved(
            resolved,
            cwd=cwd,
            env=env,
            cancel_event=cancel_event,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    async def _run_resolved(
        self,
        resolved: ResolvedSkillCommand,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
    ) -> CommandExecutionResult:
        workspace_root = self.workspace_root
        resolved_cwd = None
        if cwd is not None:
            resolved_cwd = Path(cwd).expanduser()
            if not resolved_cwd.is_absolute() and workspace_root is not None:
                resolved_cwd = (workspace_root / resolved_cwd).resolve(strict=False)

        if self.isolated_enabled and resolved.skill is not None:
            if env is None:
                result = await self.docker_executor.run(
                    resolved,
                    workspace_root=workspace_root,
                    cancel_event=cancel_event,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            else:
                result = await self.docker_executor.run(
                    resolved,
                    workspace_root=workspace_root,
                    env=env,
                    cancel_event=cancel_event,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            result.artifacts.setdefault("skill_id", resolved.skill.skill_id)
            if resolved_cwd is not None:
                result.artifacts.setdefault("cwd", str(resolved_cwd))
            return result

        parts = list(resolved.parts)
        artifacts: dict[str, object] = {}
        if parts and _is_python_command(parts[0]):
            if resolved.skill is not None:
                state = await self.env_manager.ensure_skill_env(resolved.skill)
                parts[0] = state.python_path
                artifacts["skill_id"] = resolved.skill.skill_id
                artifacts["python_path"] = state.python_path
            else:
                parts[0] = sys.executable
        if env is None:
            result = await _run_process(
                parts,
                cwd=resolved_cwd,
                timeout_seconds=self.timeout_seconds,
                max_output_bytes=self.max_output_bytes,
                mode="host",
                cancel_event=cancel_event,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        else:
            result = await _run_process(
                parts,
                cwd=resolved_cwd,
                env=env,
                timeout_seconds=self.timeout_seconds,
                max_output_bytes=self.max_output_bytes,
                mode="host",
                cancel_event=cancel_event,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        artifacts["command"] = " ".join(shlex.quote(part) for part in parts)
        if resolved_cwd is not None:
            artifacts["cwd"] = str(resolved_cwd)
        result.artifacts.update(artifacts)
        return result

    def resolve(self, command: str, *, cwd: str | None = None) -> ResolvedSkillCommand:
        parts = tuple(shlex.split(command))
        if not parts:
            return ResolvedSkillCommand(command=command, parts=parts)
        if self.skill_registry is None:
            return ResolvedSkillCommand(command=command, parts=parts)

        workspace_root = self.workspace_root
        cwd_path: Path | None = None
        if cwd is not None:
            cwd_path = Path(cwd).expanduser()
            if not cwd_path.is_absolute() and workspace_root is not None:
                cwd_path = (workspace_root / cwd_path).resolve(strict=False)

        for token in parts[1:] if _is_python_command(parts[0]) else parts:
            candidate = _resolve_token_path(token, workspace_root, cwd_path=cwd_path)
            if candidate is None:
                continue
            skill = self.skill_registry.find_by_path(candidate)
            if skill is None:
                continue
            return ResolvedSkillCommand(
                command=command,
                parts=parts,
                skill=skill,
                skill_root=Path(skill.path).resolve(strict=False).parent,
            )
        return ResolvedSkillCommand(command=command, parts=parts)

    @property
    def workspace_root(self) -> Path | None:
        if self.config is None:
            return None
        return self.config.paths.root


def _resolve_token_path(
    token: str,
    workspace_root: Path | None,
    *,
    cwd_path: Path | None = None,
) -> Path | None:
    if "/" not in token and "\\" not in token and not token.endswith(".py"):
        return None
    path = Path(token)
    if path.is_absolute():
        return path.resolve(strict=False)
    if cwd_path is not None:
        return (cwd_path / path).resolve(strict=False)
    if workspace_root is not None:
        return (workspace_root / path).resolve(strict=False)
    return path.resolve(strict=False)


def _rewrite_path_token_for_container(
    token: str,
    *,
    skill_root: Path,
    workspace_root: Path | None,
    skill_mount: Path,
    workspace_mount: Path,
) -> str:
    candidate = _resolve_token_path(token, workspace_root)
    if candidate is None:
        return token
    try:
        relative = candidate.relative_to(skill_root)
    except ValueError:
        relative = None
    if relative is not None:
        return str(skill_mount / relative)
    if workspace_root is not None:
        try:
            relative = candidate.relative_to(workspace_root)
        except ValueError:
            relative = None
        if relative is not None:
            return str(workspace_mount / relative)
    return token


def _is_python_command(token: str) -> bool:
    name = Path(token).name.lower()
    return name == "python" or name == "python3" or name.startswith("python3.")


async def _run_process(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int,
    max_output_bytes: int,
    mode: str,
    cancel_event: threading.Event | None = None,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
) -> CommandExecutionResult:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_text, stderr_text, truncated = await _communicate_with_limits(
            process,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            cancel_event=cancel_event,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    except asyncio.TimeoutError:
        _kill_process(process)
        await process.wait()
        return CommandExecutionResult(
            success=False,
            output=f"command timed out after {timeout_seconds}s",
            error=f"timed out after {timeout_seconds}s",
            exit_code=process.returncode if process.returncode is not None else -1,
            mode=mode,
        )

    detail = stdout_text.strip() or stderr_text.strip() or f"exit code {process.returncode}"
    if truncated:
        detail = f"{detail}\n[output truncated after {max_output_bytes} bytes]".strip()
        return CommandExecutionResult(
            success=False,
            output=detail,
            error=f"output limit exceeded ({max_output_bytes} bytes)",
            exit_code=process.returncode if process.returncode is not None else -1,
            mode=mode,
        )
    if process.returncode != 0:
        return CommandExecutionResult(
            success=False,
            output=detail,
            error=f"exit code {process.returncode}",
            exit_code=process.returncode,
            mode=mode,
        )
    return CommandExecutionResult(
        success=True,
        output=detail,
        error=None,
        exit_code=process.returncode,
        mode=mode,
    )


async def _communicate_with_limits(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: int,
    max_output_bytes: int,
    cancel_event: threading.Event | None = None,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
) -> tuple[str, str, bool]:
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    state = {"size": 0, "truncated": False}
    lock = asyncio.Lock()
    stdout_handle = None if stdout_path is None else Path(stdout_path).open("ab")
    stderr_handle = None if stderr_path is None else Path(stderr_path).open("ab")

    async def _read_stream(stream: asyncio.StreamReader | None, chunks: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(_MAX_CHUNK_BYTES)
            if not chunk:
                return
            async with lock:
                remaining = max_output_bytes - state["size"]
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                state["size"] += len(chunk)
                if state["size"] > max_output_bytes and not state["truncated"]:
                    state["truncated"] = True
                    _kill_process(process)
            if chunks is stdout_chunks and stdout_handle is not None:
                stdout_handle.write(chunk)
                stdout_handle.flush()
            if chunks is stderr_chunks and stderr_handle is not None:
                stderr_handle.write(chunk)
                stderr_handle.flush()

    async def _watch_cancel() -> None:
        if cancel_event is None:
            return
        while process.returncode is None:
            if cancel_event.is_set():
                _kill_process(process)
                return
            await asyncio.sleep(0.1)

    try:
        await asyncio.wait_for(
            asyncio.gather(
                _read_stream(process.stdout, stdout_chunks),
                _read_stream(process.stderr, stderr_chunks),
                process.wait(),
                _watch_cancel(),
            ),
            timeout=timeout_seconds,
        )
        return (
            b"".join(stdout_chunks).decode(errors="replace"),
            b"".join(stderr_chunks).decode(errors="replace"),
            bool(state["truncated"]),
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _kill_process(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        return
