from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
import difflib

from .models import ProposalRecord
from .skill_runtime import CommandExecutionResult, CommandRunner
from .store import SQLiteStore

_PROPOSAL_STATUS_PROPOSED = "PROPOSED"
_PROPOSAL_STATUS_APPLIED = "APPLIED"
_PROPOSAL_STATUS_TESTED = "TESTED"
_IGNORE_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", "node_modules"}
_IGNORE_PATH_PARTS = {"var", "proposals", "runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-shm"}


@dataclass(frozen=True, slots=True)
class ProposalPaths:
    root: Path
    plan: Path
    patch: Path
    tests: Path
    summary: Path
    metadata: Path
    test_results: Path


class CodexProposalService:
    def __init__(
        self,
        *,
        root: Path,
        store: SQLiteStore,
        command_runner: CommandRunner | None,
        codex_model: str,
        codex_auth_file: str = "",
    ) -> None:
        self.root = root
        self.store = store
        self.command_runner = command_runner
        self.codex_model = codex_model or "gpt-5.4"
        self.codex_auth_file = codex_auth_file.strip()

    async def propose(
        self,
        *,
        repo_path: str,
        task: str,
        context: str = "",
        files: list[str] | None = None,
    ) -> dict[str, Any]:
        repo_root = Path(repo_path).expanduser().resolve(strict=False)
        if not repo_root.exists() or not repo_root.is_dir():
            raise ValueError(f"repo_path does not exist: {repo_path}")
        proposal_id = self._proposal_id()
        proposal_paths = self._proposal_paths(proposal_id)
        proposal_paths.root.mkdir(parents=True, exist_ok=False)

        selected_files = [item for item in (files or []) if str(item).strip()]
        temp_root = Path(tempfile.mkdtemp(prefix=f"synapse-codex-{proposal_id}-"))
        base_dir = temp_root / "base"
        work_dir = temp_root / "work"
        try:
            self._copy_repo(repo_root, base_dir)
            self._copy_repo(repo_root, work_dir)
            response = await self._run_codex_exec(
                repo_root=work_dir,
                task=task,
                context=context,
                files=selected_files,
            )
            payload = self._parse_codex_payload(response)
            patch_text = self._build_patch(base_dir, work_dir)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

        plan_text = self._normalize_plan(payload.get("plan"))
        summary_text = self._normalize_text(payload.get("summary"), default="Proposal generated.")
        test_commands = self._normalize_tests(payload.get("tests"))

        proposal_paths.plan.write_text(plan_text, encoding="utf-8")
        proposal_paths.patch.write_text(patch_text, encoding="utf-8")
        proposal_paths.tests.write_text(self._render_tests(test_commands), encoding="utf-8")
        proposal_paths.summary.write_text(summary_text.rstrip() + "\n", encoding="utf-8")
        proposal_paths.metadata.write_text(
            json.dumps(
                {
                    "proposal_id": proposal_id,
                    "repo_path": str(repo_root),
                    "task": task,
                    "context": context,
                    "files": selected_files,
                    "codex_model": self.codex_model,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        record = self.store.create_codex_proposal(
            proposal_id=proposal_id,
            repo_path=str(repo_root),
            proposal_path=str(proposal_paths.root),
            task=task,
            context=context,
            files=selected_files,
            test_commands=test_commands,
            summary=summary_text.strip(),
            status=_PROPOSAL_STATUS_PROPOSED,
        )
        return {
            "proposal_id": record.proposal_id,
            "paths": self._paths_dict(proposal_paths),
            "summary": record.summary,
        }

    async def apply_proposal(self, proposal_id: str) -> dict[str, Any]:
        record = self._load_proposal(proposal_id)
        paths = self._proposal_paths(record.proposal_id)
        patch_text = paths.patch.read_text(encoding="utf-8") if paths.patch.exists() else ""
        if not patch_text.strip():
            self.store.update_codex_proposal(proposal_id, status=_PROPOSAL_STATUS_APPLIED)
            return {"proposal_id": proposal_id, "applied": False, "message": "proposal has no patch to apply"}
        result = await self._run_argv(
            ["git", "apply", str(paths.patch)],
            cwd=record.repo_path,
        )
        if not result.success:
            raise RuntimeError(result.error or result.output or "git apply failed")
        self.store.update_codex_proposal(proposal_id, status=_PROPOSAL_STATUS_APPLIED)
        return {"proposal_id": proposal_id, "applied": True, "repo_path": record.repo_path}

    async def run_tests(self, proposal_id: str) -> dict[str, Any]:
        record = self._load_proposal(proposal_id)
        commands = record.test_commands or self._parse_tests_markdown(self._proposal_paths(proposal_id).tests)
        if not commands:
            raise ValueError(f"proposal {proposal_id} does not include test commands")

        temp_root = Path(tempfile.mkdtemp(prefix=f"synapse-codex-tests-{proposal_id}-"))
        temp_repo = temp_root / "repo"
        paths = self._proposal_paths(proposal_id)
        try:
            self._copy_repo(Path(record.repo_path), temp_repo)
            patch_text = paths.patch.read_text(encoding="utf-8") if paths.patch.exists() else ""
            if patch_text.strip():
                apply_result = await self._run_argv(["git", "apply", str(paths.patch)], cwd=str(temp_repo))
                if not apply_result.success:
                    raise RuntimeError(apply_result.error or apply_result.output or "git apply failed in test sandbox")

            results: list[dict[str, Any]] = []
            for command in commands:
                command_result = await self._run_command(command, cwd=str(temp_repo))
                results.append(
                    {
                        "command": command,
                        "success": command_result.success,
                        "output": command_result.output,
                        "error": command_result.error,
                        "exit_code": command_result.exit_code,
                    }
                )
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

        paths.test_results.write_text(self._render_test_results(results), encoding="utf-8")
        self.store.update_codex_proposal(proposal_id, status=_PROPOSAL_STATUS_TESTED)
        return {
            "proposal_id": proposal_id,
            "results": results,
            "results_path": str(paths.test_results),
        }

    def _load_proposal(self, proposal_id: str) -> ProposalRecord:
        record = self.store.get_codex_proposal(proposal_id)
        if record is None:
            raise KeyError(f"unknown proposal_id: {proposal_id}")
        return record

    def _proposal_paths(self, proposal_id: str) -> ProposalPaths:
        root = self.root / "var" / "proposals" / proposal_id
        return ProposalPaths(
            root=root,
            plan=root / "PLAN.md",
            patch=root / "PATCH.diff",
            tests=root / "TESTS.md",
            summary=root / "SUMMARY.md",
            metadata=root / "METADATA.json",
            test_results=root / "TEST_RESULTS.md",
        )

    def _proposal_id(self) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{timestamp}-{uuid4().hex[:8]}"

    async def _run_codex_exec(
        self,
        *,
        repo_root: Path,
        task: str,
        context: str,
        files: list[str],
    ) -> str:
        prompt = self._build_prompt(task=task, context=context, files=files)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as output_file:
            output_path = Path(output_file.name)
        try:
            result = await self._run_argv(
                [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "-C",
                    str(repo_root),
                    "-m",
                    self.codex_model,
                    "-o",
                    str(output_path),
                    prompt,
                ],
                cwd=str(repo_root),
                env=self._codex_env(),
            )
            if not result.success:
                raise RuntimeError(result.error or result.output or "codex exec failed")
            response = output_path.read_text(encoding="utf-8").strip()
            if not response:
                raise RuntimeError("codex exec returned an empty response")
            return response
        finally:
            output_path.unlink(missing_ok=True)

    def _build_prompt(self, *, task: str, context: str, files: list[str]) -> str:
        lines = [
            "You are preparing a proposal-only code change in a temporary clone of the user's repository.",
            "You may edit files in this temporary clone to express the proposal.",
            "Do not print markdown fences.",
            "Return JSON only with keys: plan, summary, tests.",
            "plan must be an array of short strings.",
            "summary must be a short markdown summary string.",
            "tests must be an array of shell commands that verify the change.",
            "",
            f"Task: {task.strip()}",
        ]
        if context.strip():
            lines.extend(["", "Additional context:", context.strip()])
        if files:
            lines.extend(["", "Focus files:", *[f"- {item}" for item in files]])
        lines.extend(
            [
                "",
                "Modify the temporary repository as needed, then return the JSON payload.",
                "Do not include explanations outside the JSON object.",
            ]
        )
        return "\n".join(lines)

    def _codex_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CODEX_MODEL"] = self.codex_model
        if self.codex_auth_file:
            env["CODEX_AUTH_FILE"] = self.codex_auth_file
        return env

    async def _run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandExecutionResult:
        if self.command_runner is not None:
            return await self.command_runner.run_argv(argv, cwd=cwd, env=env)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        output = (stdout or b"").decode(errors="replace").strip()
        error_text = (stderr or b"").decode(errors="replace").strip()
        detail = output or error_text or f"exit code {process.returncode}"
        return CommandExecutionResult(
            success=process.returncode == 0,
            output=detail,
            error=None if process.returncode == 0 else f"exit code {process.returncode}",
            exit_code=process.returncode or 0,
            mode="host",
            artifacts={"command": argv, "cwd": cwd or ""},
        )

    async def _run_command(self, command: str, *, cwd: str | None = None) -> CommandExecutionResult:
        if self.command_runner is not None:
            return await self.command_runner.run(command, cwd=cwd)
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        output = (stdout or b"").decode(errors="replace").strip()
        error_text = (stderr or b"").decode(errors="replace").strip()
        detail = output or error_text or f"exit code {process.returncode}"
        return CommandExecutionResult(
            success=process.returncode == 0,
            output=detail,
            error=None if process.returncode == 0 else f"exit code {process.returncode}",
            exit_code=process.returncode or 0,
            mode="host",
            artifacts={"command": command, "cwd": cwd or ""},
        )

    def _copy_repo(self, source: Path, target: Path) -> None:
        shutil.copytree(source, target, ignore=self._ignore_paths)

    def _ignore_paths(self, directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        ignored: set[str] = set()
        for name in names:
            candidate = base / name
            if name in _IGNORE_DIRS:
                ignored.add(name)
                continue
            parts = candidate.parts
            # Ignore var/proposals subtree.
            if len(parts) >= 2 and tuple(parts[-2:]) == ("var", "proposals"):
                ignored.add(name)
                continue
            # Ignore runtime sqlite artifacts wherever they appear.
            if name in {"runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-shm"}:
                ignored.add(name)
        return ignored

    def _parse_codex_payload(self, response: str) -> dict[str, Any]:
        try:
            payload = json.loads(response)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            payload = json.loads(match.group(0))
            if isinstance(payload, dict):
                return payload
        raise ValueError("codex response was not valid JSON")

    def _normalize_plan(self, plan: Any) -> str:
        if isinstance(plan, list):
            items = [str(item).strip() for item in plan if str(item).strip()]
        elif isinstance(plan, str) and plan.strip():
            items = [plan.strip()]
        else:
            items = ["Review the task and proposed changes."]
        body = "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))
        return "# Plan\n\n" + body + "\n"

    def _normalize_text(self, value: Any, *, default: str) -> str:
        text = str(value or "").strip()
        return text or default

    def _normalize_tests(self, tests: Any) -> list[str]:
        if isinstance(tests, list):
            return [str(item).strip() for item in tests if str(item).strip()]
        if isinstance(tests, str) and tests.strip():
            return [tests.strip()]
        return []

    def _render_tests(self, commands: list[str]) -> str:
        lines = ["# Tests", ""]
        if not commands:
            lines.append("No test commands were proposed.")
        else:
            lines.extend(f"- `{command}`" for command in commands)
        return "\n".join(lines) + "\n"

    def _render_test_results(self, results: list[dict[str, Any]]) -> str:
        lines = ["# Test Results", ""]
        for result in results:
            status = "PASS" if result["success"] else "FAIL"
            lines.append(f"## {status} `{result['command']}`")
            lines.append("")
            lines.append("```text")
            lines.append(str(result["output"] or result["error"] or "").rstrip())
            lines.append("```")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _parse_tests_markdown(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        commands: list[str] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("- `") and line.endswith("`"):
                commands.append(line[3:-1])
        return commands

    def _paths_dict(self, paths: ProposalPaths) -> dict[str, str]:
        return {
            "root": str(paths.root),
            "plan": str(paths.plan),
            "patch": str(paths.patch),
            "tests": str(paths.tests),
            "summary": str(paths.summary),
        }

    def _build_patch(self, base_dir: Path, work_dir: Path) -> str:
        base_files = self._file_map(base_dir)
        work_files = self._file_map(work_dir)
        patch_chunks: list[str] = []
        for relative in sorted(set(base_files) | set(work_files)):
            before = base_files.get(relative)
            after = work_files.get(relative)
            if before == after:
                continue
            patch = self._diff_file(relative, before, after)
            if patch:
                patch_chunks.append(patch)
        return "".join(patch_chunks)

    def _file_map(self, root: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in _IGNORE_DIRS for part in relative.parts):
                continue
            text = self._read_text(path)
            if text is None:
                continue
            files[relative.as_posix()] = text
        return files

    def _read_text(self, path: Path) -> str | None:
        data = path.read_bytes()
        if b"\0" in data:
            return None
        return data.decode("utf-8", errors="replace")

    def _diff_file(self, relative: str, before: str | None, after: str | None) -> str:
        if before is None and after is None:
            return ""
        header = [f"diff --git a/{relative} b/{relative}\n"]
        if before is None:
            header.append("new file mode 100644\n")
            diff_lines = difflib.unified_diff(
                [],
                after.splitlines(keepends=True) if after is not None else [],
                fromfile="/dev/null",
                tofile=f"b/{relative}",
            )
        elif after is None:
            header.append("deleted file mode 100644\n")
            diff_lines = difflib.unified_diff(
                before.splitlines(keepends=True),
                [],
                fromfile=f"a/{relative}",
                tofile="/dev/null",
            )
        else:
            diff_lines = difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        body = "".join(diff_lines)
        return "".join(header) + body
