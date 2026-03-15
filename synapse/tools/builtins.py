"""Builtin tools — memory, self-awareness, web, shell, skills, reminders."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shlex
import shutil
import tempfile
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any
from urllib.parse import urlparse
import re

import httpx

from ..codex_tools import CodexProposalService
from ..skill_runtime import DependencyInstallDisabledError
from .registry import ToolContext, ToolDef, ToolRegistry, ToolResult

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = re.compile(
    r"^(localhost|127\.|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|169\.254\.|0\.|::1|fc00:|fe80:|fd)",
    re.IGNORECASE,
)
_SAFE_SHELL_COMMANDS = {"pwd", "ls", "whoami"}
_GWS_WRITE_KEYWORDS = {"send", "create", "delete", "remove", "insert", "update", "patch", "trash"}
_SAFE_GIT_SUBCOMMANDS = {"status", "diff"}
# Read-only inspection commands that should not require approval.
_SAFE_READONLY_COMMANDS = {"rg", "grep", "cat", "head", "tail", "stat", "sed"}
_SAFE_WRITE_DIRS = ("skills", "var/proposals")


def _git_subcommand(parts: list[str]) -> str | None:
    if not parts or parts[0] != "git":
        return None
    index = 1
    while index < len(parts):
        token = parts[index]
        if token in {"-C", "--git-dir", "--work-tree", "-c"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def _is_safe_git_command(parts: list[str]) -> bool:
    subcommand = _git_subcommand(parts)
    if subcommand not in _SAFE_GIT_SUBCOMMANDS:
        return False
    return True


def _shell_needs_approval(params: dict[str, Any]) -> bool:
    command = str(params.get("command", "")).strip()
    parts = shlex.split(command) if command else []
    first = parts[0] if parts else ""

    # Hard allowlist: swing-trader scanner read-only operations should never require approval.
    # This prevents UX friction for technical analysis.
    lowered = command.lower()
    if "skills/swing-trader/scripts/scanner.py" in lowered:
        if " analyze " in f" {lowered} " or " scan " in f" {lowered} ":
            return False

    if first in _SAFE_SHELL_COMMANDS:
        return False
    if _is_safe_git_command(parts):
        return False
    if first in _SAFE_READONLY_COMMANDS:
        # Restrict sed: only allow non-mutating `sed -n` usage.
        if first == "sed":
            return "-n" not in parts
        return False
    if first == "gws":
        # GWS read commands are safe; write commands need approval
        return any(kw in command.lower() for kw in _GWS_WRITE_KEYWORDS)
    return True


def _workspace_root(config: Any | None) -> Path:
    if config is not None and getattr(config, "paths", None) is not None:
        return Path(config.paths.root).expanduser().resolve(strict=False)
    return Path.cwd().resolve(strict=False)


def _filesystem_allow_absolute(config: Any | None) -> bool:
    fs_config = getattr(config, "filesystem", None)
    return bool(getattr(fs_config, "allow_absolute", False))


def _filesystem_requires_approval(config: Any | None) -> bool:
    fs_config = getattr(config, "filesystem", None)
    return bool(getattr(fs_config, "require_approval", False))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_workspace_path(
    raw_path: str,
    *,
    config: Any | None,
    enforce_repo_root: bool = False,
) -> Path:
    path_text = str(raw_path).strip()
    if not path_text:
        raise ValueError("missing path")
    root = _workspace_root(config)
    path = Path(path_text).expanduser()
    resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
    if enforce_repo_root or not _filesystem_allow_absolute(config):
        if not _is_relative_to(resolved, root):
            raise ValueError("path is outside the workspace root")
    return resolved


def _resolve_repo_cwd(
    raw_cwd: str | None,
    *,
    config: Any | None,
) -> Path:
    cwd_text = str(raw_cwd).strip() if raw_cwd is not None else ""
    if not cwd_text:
        return _workspace_root(config)
    return _resolve_workspace_path(cwd_text, config=config, enforce_repo_root=True)


def _line_bounds(text: str, start_index: int, segment: str) -> dict[str, int]:
    start_line = text[:start_index].count("\n") + 1
    end_line = start_line + segment.count("\n")
    return {"from": start_line, "to": end_line}


def _safe_write_path(path: Path, *, config: Any | None) -> bool:
    root = _workspace_root(config)
    if not _is_relative_to(path, root):
        return False
    for prefix in _SAFE_WRITE_DIRS:
        if _is_relative_to(path, root / prefix):
            return True
    return False


def _fs_mutation_needs_approval(config: Any | None):
    def _needs_approval(params: dict[str, Any]) -> bool:
        if _filesystem_requires_approval(config):
            return True
        try:
            path = _resolve_workspace_path(str(params.get("path", "")), config=config)
        except ValueError:
            return True
        return not _safe_write_path(path, config=config)

    return _needs_approval


# --- Memory tools ---


async def _memory_read(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    scope = str(params.get("scope", "all"))
    sections: list[str] = []
    if scope in {"all", "user"}:
        text = ctx.memory.read_user_memory(ctx.user_id)
        sections.append(f"## User Memory\n{text}" if text else "## User Memory\n(empty)")
    if scope in {"all", "session"}:
        notes = ctx.memory.read_session_notes(ctx.session_key)
        summary = ctx.memory.read_session_summary(ctx.session_key)
        transcript = ctx.memory.read_recent_transcript(ctx.session_key)
        transcript_text = "\n".join(
            f"{e.get('role', '?')}: {e.get('content', '')}" for e in transcript
        ).strip()
        sections.append(f"## Session Notes\n{notes}")
        if summary:
            sections.append(f"## Session Summary\n{summary}")
        if transcript_text:
            sections.append(f"## Recent Transcript\n{transcript_text}")
    if scope in {"all", "global"}:
        text = ctx.memory.read_global_memory()
        sections.append(f"## Global Memory\n{text}" if text else "## Global Memory\n(empty)")
    return ToolResult(output="\n\n".join(sections) or "(no memory)")


async def _memory_write(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    scope = str(params.get("scope", "session"))
    content = str(params.get("content", "")).strip()
    if not content:
        return ToolResult(output="", error="missing content")
    if scope == "session":
        ctx.memory.append_notes(ctx.session_key, content)
    elif scope == "user":
        ctx.memory.append_user_memory(ctx.user_id, content)
    elif scope == "global":
        name = str(params.get("name", "general"))
        ctx.memory.append_global_memory(content, name=name)
    else:
        return ToolResult(output="", error=f"invalid scope: {scope}")
    return ToolResult(output=f"{scope} memory updated")


async def _memory_delete(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    scope = str(params.get("scope", "session"))
    content = str(params.get("content", "")).strip()
    if scope == "session":
        removed = ctx.memory.delete_session_notes(ctx.session_key, content)
    elif scope == "user":
        removed = ctx.memory.delete_user_memory(ctx.user_id, content)
    elif scope == "global":
        name = str(params.get("name", "general"))
        removed = ctx.memory.delete_global_memory(content, name=name)
    else:
        return ToolResult(output="", error=f"invalid scope: {scope}")
    return ToolResult(output=f"{scope} memory {'removed' if removed else 'item not found'}")


async def _memory_search(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    query = str(params.get("query", "")).strip()
    scope = str(params.get("scope", "all"))
    if not query:
        return ToolResult(output="", error="missing search query")
    results = ctx.memory.search(query, scope=scope)
    if results:
        lines = [f"- [{r.source}:{r.line}] {r.content}" for r in results]
        return ToolResult(output="\n".join(lines))
    return ToolResult(output="no results found")


# --- Self-awareness tools ---


async def _self_describe(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    return ToolResult(
        output="I am Synapse: an async Python agent runtime with explicit state machines, "
        "approval gates, and plug-and-play skill/MCP integration. "
        "I assist with Google Workspace, memory, web search, and more."
    )


async def _self_health(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    runs = ctx.store.list_runs(limit=200)
    total = len(runs)
    state_counts: dict[str, int] = {}
    for run in runs:
        state_counts[run.state.value] = state_counts.get(run.state.value, 0) + 1
    completed = state_counts.get("COMPLETED", 0)
    failed = state_counts.get("FAILED", 0)
    rate = failed / total if total > 0 else 0.0
    return ToolResult(
        output=f"Health: {total} runs, {completed} completed, {failed} failed ({rate:.1%} failure rate)",
        artifacts={"total": total, "completed": completed, "failed": failed, "failure_rate": round(rate, 3)},
    )


async def _self_capabilities(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    return ToolResult(output="Use the available tools to interact with the runtime. All capabilities are exposed as tools.")


async def _diagnosis_report(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    engine = getattr(ctx, "diagnosis_engine", None)
    if engine is None:
        return ToolResult(output="", error="diagnosis engine not configured")
    window_hours = int(params.get("window_hours", 24))
    report = engine.analyze_runs(window_hours=window_hours)
    lines = [
        f"Health score: {report.health_score:.2f}",
        f"Total runs: {report.total_runs} ({report.completed_runs} completed, {report.failed_runs} failed)",
    ]
    if report.gaps:
        lines.append(f"Gaps: {len(report.gaps)}")
        for gap in report.gaps:
            lines.append(f"  - {gap.description} (freq: {gap.frequency})")
    if report.improvements:
        lines.append(f"Suggestions: {len(report.improvements)}")
        for imp in report.improvements:
            lines.append(f"  - [{imp.priority}] {imp.suggestion}")
    return ToolResult(output="\n".join(lines))


# --- Web tools ---


async def _web_search(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    query = str(params.get("query", "")).strip()
    if not query:
        return ToolResult(output="", error="missing search query")
    try:
        prompt = (
            "Answer the user's request using live web research.\n"
            "Use web search when needed.\n"
            "Return a concise answer with short source citations.\n\n"
            f"Query: {query}"
        )
        with tempfile.NamedTemporaryFile(mode="r+", encoding="utf-8", suffix=".txt") as f:
            cmd = ["codex", "exec", "--skip-git-repo-check", "-m", "gpt-5.4", "-o", f.name, prompt]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0:
                detail = stderr.decode().strip() or stdout.decode().strip()
                return ToolResult(output="", error=f"web search failed: {detail}")
            f.seek(0)
            answer = f.read().strip()
        return ToolResult(output=answer or "no results")
    except Exception as e:
        return ToolResult(output="", error=f"web search failed: {e}")


async def _web_fetch(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    url = str(params.get("url", "")).strip()
    if not url:
        return ToolResult(output="", error="missing url")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return ToolResult(output="", error=f"disallowed scheme: {parsed.scheme}")
    host = parsed.hostname or ""
    if _BLOCKED_HOSTS.match(host):
        return ToolResult(output="", error=f"disallowed host: {host}")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        snippet = resp.text[:2000]
        return ToolResult(output=f"Status {resp.status_code}:\n{snippet}")
    except Exception as e:
        return ToolResult(output="", error=f"fetch failed: {e}")


# --- Shell ---


async def _run_command(command: str, *, ctx: ToolContext, cwd: str | None = None) -> ToolResult:
    if ctx.command_runner is not None:
        runner_kwargs: dict[str, Any] = {}
        if getattr(ctx, "cancel_event", None) is not None:
            runner_kwargs["cancel_event"] = getattr(ctx, "cancel_event")
        if getattr(ctx, "stdout_path", None):
            runner_kwargs["stdout_path"] = getattr(ctx, "stdout_path")
        if getattr(ctx, "stderr_path", None):
            runner_kwargs["stderr_path"] = getattr(ctx, "stderr_path")
        try:
            result = await ctx.command_runner.run(command, cwd=cwd, **runner_kwargs)
        except DependencyInstallDisabledError as error:
            return ToolResult(output="", error=str(error))
        except Exception as error:
            return ToolResult(output="", error=f"shell exec failed: {error}")
        artifacts = dict(result.artifacts)
        artifacts.setdefault("mode", result.mode)
        artifacts.setdefault("exit_code", result.exit_code)
        if cwd is not None:
            artifacts.setdefault("cwd", cwd)
        return ToolResult(output=result.output, error=result.error, artifacts=artifacts)

    parts = shlex.split(command)
    if parts and parts[0] in ("python3", "python"):
        import sys

        parts[0] = sys.executable
    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        out = stdout.decode() if stdout else ""
        err = stderr.decode() if stderr else ""
        detail = out.strip() or err.strip() or f"exit code {proc.returncode}"
        artifacts: dict[str, Any] = {}
        if cwd is not None:
            artifacts["cwd"] = cwd
        if proc.returncode != 0:
            return ToolResult(output=detail, error=f"exit code {proc.returncode}", artifacts=artifacts or None)
        return ToolResult(output=detail, artifacts=artifacts or None)
    except Exception as e:
        return ToolResult(output="", error=f"shell exec failed: {e}")


async def _run_argv_command(argv: list[str], *, ctx: ToolContext, cwd: str | None = None) -> ToolResult:
    if ctx.command_runner is not None:
        runner_kwargs: dict[str, Any] = {}
        if getattr(ctx, "cancel_event", None) is not None:
            runner_kwargs["cancel_event"] = getattr(ctx, "cancel_event")
        if getattr(ctx, "stdout_path", None):
            runner_kwargs["stdout_path"] = getattr(ctx, "stdout_path")
        if getattr(ctx, "stderr_path", None):
            runner_kwargs["stderr_path"] = getattr(ctx, "stderr_path")
        try:
            result = await ctx.command_runner.run_argv(argv, cwd=cwd, **runner_kwargs)
        except DependencyInstallDisabledError as error:
            return ToolResult(output="", error=str(error))
        except Exception as error:
            return ToolResult(output="", error=f"command failed: {error}")
        artifacts = dict(result.artifacts)
        artifacts.setdefault("mode", result.mode)
        artifacts.setdefault("exit_code", result.exit_code)
        if cwd is not None:
            artifacts.setdefault("cwd", cwd)
        return ToolResult(output=result.output, error=result.error, artifacts=artifacts)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        out = stdout.decode() if stdout else ""
        err = stderr.decode() if stderr else ""
        detail = out.strip() or err.strip() or f"exit code {proc.returncode}"
        artifacts: dict[str, Any] = {}
        if cwd is not None:
            artifacts["cwd"] = cwd
        if proc.returncode != 0:
            return ToolResult(output=detail, error=f"exit code {proc.returncode}", artifacts=artifacts or None)
        return ToolResult(output=detail, artifacts=artifacts or None)
    except Exception as error:
        return ToolResult(output="", error=f"command failed: {error}")


def _enqueue_background_tool(tool_name: str, params: dict[str, Any], *, ctx: ToolContext) -> ToolResult | None:
    if not bool(params.get("background")):
        return None
    if getattr(ctx, "job_id", None):
        return None
    if getattr(ctx, "job_service", None) is None:
        return ToolResult(output="", error="background jobs are not available")
    payload = {key: value for key, value in params.items() if key != "background"}
    job = ctx.job_service.enqueue_job(
        tool_name=tool_name,
        params=payload,
        parent_run_id=getattr(ctx, "run_id", None),
        session_key=ctx.session_key or None,
        delivery_target=getattr(ctx, "delivery_target", None),
        approval_id=getattr(ctx, "approval_id", None),
    )
    job_artifacts = ctx.job_service.artifact_paths(job.job_id)
    return ToolResult(
        output=f"Started background job `{job.job_id}` for `{tool_name}`.",
        artifacts={
            "job_id": job.job_id,
            "status": job.status.value,
            "artifact_root": job.artifact_root,
            "request_path": str(job_artifacts.request_path),
            "progress_path": str(job_artifacts.progress_path),
            "result_path": str(job_artifacts.result_path),
            "stdout_path": str(job_artifacts.stdout_path),
            "stderr_path": str(job_artifacts.stderr_path),
            "summary_path": str(job_artifacts.summary_path),
        },
    )


async def _shell_exec(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    command = str(params.get("command", "")).strip()
    if not command:
        return ToolResult(output="", error="missing command")

    # Guard against placeholder leaks (e.g. @@CODE0@@) that sometimes come from
    # markdown/code-block renderers. These are not executable commands.
    if "@@" in command and re.search(r"@@[A-Z]+[0-9_]*@@", command):
        return ToolResult(
            output="",
            error=(
                "command contains placeholder tokens (e.g. @@CODE0@@). "
                "Please paste the raw command text (no placeholders), or wrap the command in bash -lc."
            ),
        )

    cwd_raw = params.get("cwd")
    cwd = str(cwd_raw).strip() if isinstance(cwd_raw, str) and cwd_raw.strip() else None
    background_result = _enqueue_background_tool("shell_exec", {"command": command, "cwd": cwd, "background": params.get("background", False)}, ctx=ctx)
    if background_result is not None:
        return background_result
    return await _run_command(command, ctx=ctx, cwd=cwd)


async def _shell_readonly(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    """Read-only shell execution with a strict allowlist (no approval needed)."""
    command = str(params.get("command", "")).strip()
    if not command:
        return ToolResult(output="", error="missing command")

    if "@@" in command and re.search(r"@@[A-Z]+[0-9_]*@@", command):
        return ToolResult(output="", error="command contains placeholder tokens; paste raw command text")

    parts = shlex.split(command)
    if not parts:
        return ToolResult(output="", error="missing command")

    first = parts[0]
    allowed = False
    if first in _SAFE_SHELL_COMMANDS:
        allowed = True
    elif first == "git" and _is_safe_git_command(parts):
        allowed = True
    elif first in _SAFE_READONLY_COMMANDS:
        allowed = True

    if not allowed:
        return ToolResult(
            output="",
            error=(
                "shell_readonly only allows: pwd, ls, whoami, git status/diff, rg, grep. "
                "Use shell_exec (approval) for anything else."
            ),
        )

    cwd_raw = params.get("cwd")
    try:
        cwd_path = _resolve_repo_cwd(cwd_raw if isinstance(cwd_raw, str) else None, config=ctx.config)
        cwd = str(cwd_path)
    except Exception:
        cwd = str(cwd_raw).strip() if isinstance(cwd_raw, str) and cwd_raw.strip() else None
    return await _run_command(command, ctx=ctx, cwd=cwd)


async def _fs_read_impl(
    params: dict[str, Any],
    *,
    ctx: ToolContext,
    enforce_repo_root: bool,
) -> ToolResult:
    try:
        path = _resolve_workspace_path(str(params.get("path", "")), config=ctx.config, enforce_repo_root=enforce_repo_root)
    except ValueError as error:
        return ToolResult(output="", error=str(error))
    if not path.exists():
        return ToolResult(output="", error=f"path does not exist: {path}")
    if not path.is_file():
        return ToolResult(output="", error=f"path is not a file: {path}")

    start = int(params.get("from", 1))
    count_raw = params.get("lines")
    if start < 1:
        return ToolResult(output="", error="from must be >= 1")
    if count_raw is not None and int(count_raw) < 1:
        return ToolResult(output="", error="lines must be >= 1")

    text = path.read_text(encoding="utf-8")
    all_lines = text.splitlines()
    end = len(all_lines) if count_raw is None else min(len(all_lines), start - 1 + int(count_raw))
    selected = all_lines[start - 1:end]
    output = "\n".join(selected)
    line_to = end if selected else start - 1
    return ToolResult(
        output=output,
        artifacts={
            "path": str(path),
            "bytes_read": len(output.encode("utf-8")),
            "line_range": {"from": start, "to": line_to},
        },
    )


async def _fs_read(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    return await _fs_read_impl(params, ctx=ctx, enforce_repo_root=False)


async def _repo_open(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    return await _fs_read_impl(params, ctx=ctx, enforce_repo_root=True)


async def _fs_write(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    content = str(params.get("content", ""))
    mkdirp = bool(params.get("mkdirp", False))
    try:
        path = _resolve_workspace_path(str(params.get("path", "")), config=ctx.config)
    except ValueError as error:
        return ToolResult(output="", error=str(error))

    parent = path.parent
    if not parent.exists():
        if not mkdirp:
            return ToolResult(output="", error=f"parent directory does not exist: {parent}")
        parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    changed = previous != content
    path.write_text(content, encoding="utf-8")
    return ToolResult(
        output=f"Wrote {path}",
        artifacts={
            "path": str(path),
            "bytes_written": len(content.encode("utf-8")),
            "changed": changed,
        },
    )


async def _fs_edit(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    old = str(params.get("old", ""))
    new = str(params.get("new", ""))
    if not old:
        return ToolResult(output="", error="missing old")
    try:
        path = _resolve_workspace_path(str(params.get("path", "")), config=ctx.config)
    except ValueError as error:
        return ToolResult(output="", error=str(error))
    if not path.exists():
        return ToolResult(output="", error=f"path does not exist: {path}")
    if not path.is_file():
        return ToolResult(output="", error=f"path is not a file: {path}")

    original = path.read_text(encoding="utf-8")
    match_count = original.count(old)
    if match_count != 1:
        return ToolResult(output="", error=f"old text must match exactly once; found {match_count} matches")
    start_index = original.index(old)
    line_range = _line_bounds(original, start_index, old)
    updated = original.replace(old, new, 1)
    changed = updated != original
    if changed:
        path.write_text(updated, encoding="utf-8")
    return ToolResult(
        output=f"Edited {path}",
        artifacts={
            "path": str(path),
            "bytes_written": len(updated.encode("utf-8")),
            "changed": changed,
            "line_range": line_range,
        },
    )


async def _patch_apply(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    patch_text = str(params.get("patch", ""))
    if not patch_text.strip():
        return ToolResult(output="", error="missing patch")

    try:
        cwd_path = _resolve_repo_cwd(params.get("cwd"), config=ctx.config)
    except ValueError as error:
        return ToolResult(output="", error=str(error))

    proposal_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
    proposal_root = Path(ctx.config.paths.root) / "var" / "proposals" / "manual" / proposal_id
    proposal_root.mkdir(parents=True, exist_ok=False)
    patch_path = proposal_root / "PATCH.diff"
    metadata_path = proposal_root / "METADATA.json"
    patch_path.write_text(patch_text, encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {
                "proposal_id": proposal_id,
                "tool": "patch_apply",
                "cwd": str(cwd_path),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "patch_path": str(patch_path),
                "bytes_written": len(patch_text.encode("utf-8")),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = await _run_argv_command(["git", "apply", str(patch_path)], ctx=ctx, cwd=str(cwd_path))
    artifacts = dict(result.artifacts or {})
    artifacts.update(
        {
            "proposal_id": proposal_id,
            "proposal_path": str(proposal_root),
            "patch_path": str(patch_path),
            "metadata_path": str(metadata_path),
            "bytes_written": len(patch_text.encode("utf-8")),
            "applied": result.error is None,
        }
    )
    if result.error:
        return ToolResult(output=result.output, error=result.error, artifacts=artifacts)
    return ToolResult(output=result.output or f"Applied patch proposal {proposal_id}", artifacts=artifacts)


async def _repo_status(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    cwd = str(params.get("cwd", "")).strip() or None
    return await _run_command("git status -sb", ctx=ctx, cwd=cwd)


async def _repo_diffstat(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    cwd = str(params.get("cwd", "")).strip() or None
    return await _run_command("git diff --stat", ctx=ctx, cwd=cwd)


async def _repo_diff(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    cwd = str(params.get("cwd", "")).strip() or None
    return await _run_command("git diff", ctx=ctx, cwd=cwd)


async def _repo_grep(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    query = str(params.get("query", "")).strip()
    if not query:
        return ToolResult(output="", error="missing query")
    try:
        cwd_path = _resolve_repo_cwd(params.get("cwd"), config=ctx.config)
    except ValueError as error:
        return ToolResult(output="", error=str(error))

    max_matches = params.get("max")
    argv = ["rg", "-n", "--color", "never"]
    if params.get("glob"):
        argv.extend(["--glob", str(params["glob"])])
    if max_matches is not None:
        argv.extend(["--max-count", str(int(max_matches))])
    argv.extend([query, "."])

    if shutil.which("rg") is None:
        argv = ["grep", "-RIn"]
        if params.get("glob"):
            argv.extend(["--include", str(params["glob"])])
        if max_matches is not None:
            argv.extend(["-m", str(int(max_matches))])
        argv.extend([query, "."])

    result = await _run_argv_command(argv, ctx=ctx, cwd=str(cwd_path))
    artifacts = dict(result.artifacts or {})
    artifacts.setdefault("cwd", str(cwd_path))
    artifacts["command"] = argv
    return ToolResult(output=result.output, error=result.error, artifacts=artifacts)


# --- Skills ---


async def _load_skill(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    skill_id = str(params.get("skill_id", "")).strip()
    if not skill_id:
        return ToolResult(output="", error="missing skill_id")
    if ctx.skill_registry is None:
        return ToolResult(output="", error="skill registry not available")
    skill = ctx.skill_registry.get(skill_id)
    if skill is None:
        return ToolResult(output="", error=f"skill not found: {skill_id}")
    return ToolResult(
        output=f"# Skill: {skill.name}\n\n{skill.instruction_markdown}",
        artifacts={"skill_id": skill_id, "path": getattr(skill, "path", "")},
    )


def _swing_skill_root(ctx: ToolContext) -> Path:
    if ctx.skill_registry is None:
        raise ValueError("skill registry not available")
    skill = ctx.skill_registry.get("swing-trader")
    if skill is None:
        raise ValueError("skill not found: swing-trader")
    if not skill.path:
        raise ValueError("skill path unavailable: swing-trader")
    return Path(skill.path).resolve(strict=False).parent


def _extract_json_payload(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    start_obj = text.find("{")
    end_obj = text.rfind("}")
    if start_obj != -1 and end_obj != -1 and start_obj < end_obj:
        candidate = text[start_obj : end_obj + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    start_arr = text.find("[")
    end_arr = text.rfind("]")
    if start_arr != -1 and end_arr != -1 and start_arr < end_arr:
        candidate = text[start_arr : end_arr + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return None


def _apply_scan_mode(payload: Any, mode: str, top: int | None) -> Any:
    if not isinstance(payload, dict):
        return payload
    setups = payload.get("setups")
    if not isinstance(setups, list):
        return payload

    selected = list(setups)
    def _safe_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if mode == "trade_ready":
        selected = [
            item
            for item in selected
            if isinstance(item, dict)
            and bool(item.get("filters", {}).get("pass"))
            and (
                not isinstance(item.get("risk_reward"), dict)
                or not isinstance(item["risk_reward"].get("targets"), list)
                or not item["risk_reward"]["targets"]
                or _safe_float(item["risk_reward"]["targets"][0].get("rr", 0)) >= 1.5
            )
        ]
    elif mode == "near_setups":
        selected = [
            item
            for item in selected
            if isinstance(item, dict)
            and (
                bool(item.get("combo"))
                or bool(item.get("filters", {}).get("rsi_ok"))
                or _safe_float(item.get("change_pct", 0)) > -2.0
            )
        ]

    if top is not None and top > 0:
        selected = selected[:top]

    filtered = dict(payload)
    filtered["mode"] = mode
    filtered["setups"] = selected
    filtered["setups_found"] = len(selected)
    return filtered


async def _swing_analyze(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    symbol = re.sub(r"[^A-Za-z0-9]", "", str(params.get("symbol", "")).strip().upper())
    timeframe = str(params.get("timeframe", "daily")).strip().lower() or "daily"
    if not symbol:
        return ToolResult(output="", error="missing symbol")
    if timeframe not in {"1m", "5m", "15m", "1h", "4h", "daily", "weekly", "monthly"}:
        return ToolResult(output="", error=f"invalid timeframe: {timeframe}")

    try:
        skill_root = _swing_skill_root(ctx)
    except ValueError as error:
        return ToolResult(output="", error=str(error))

    script_path = skill_root / "scripts" / "scanner.py"
    argv = ["python3", str(script_path), "analyze", "--symbol", symbol, "--timeframe", timeframe]
    result = await _run_argv_command(argv, ctx=ctx, cwd=str(skill_root))
    parsed = _extract_json_payload(result.output)
    response = {"parsed": parsed, "raw": result.output}
    artifacts = dict(result.artifacts or {})
    artifacts.update(
        {
            "skill_id": "swing-trader",
            "skill_root": str(skill_root),
            "argv": argv,
            "parsed": parsed,
            "raw": result.output,
        }
    )
    return ToolResult(output=json.dumps(response), error=result.error, artifacts=artifacts)


async def _swing_scan(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    pattern = str(params.get("pattern", "all")).strip() or "all"
    watchlist = str(params.get("watchlist", "nifty50")).strip() or "nifty50"
    mode = str(params.get("mode", "trade_ready")).strip().lower() or "trade_ready"
    top = params.get("top")
    if mode not in {"trade_ready", "near_setups"}:
        return ToolResult(output="", error=f"invalid mode: {mode}")
    try:
        top_value = int(top) if top is not None else None
    except (TypeError, ValueError):
        return ToolResult(output="", error="top must be an integer")
    if top_value is not None and top_value < 1:
        return ToolResult(output="", error="top must be >= 1")

    try:
        skill_root = _swing_skill_root(ctx)
    except ValueError as error:
        return ToolResult(output="", error=str(error))

    script_path = skill_root / "scripts" / "scanner.py"
    argv = ["python3", str(script_path), "scan", "--pattern", pattern, "--watchlist", watchlist]
    if mode:
        argv.extend(["--mode", mode])
    if top_value is not None:
        argv.extend(["--top", str(top_value)])

    result = await _run_argv_command(argv, ctx=ctx, cwd=str(skill_root))
    fallback_argv: list[str] | None = None
    if result.error and ("unrecognized arguments" in result.output or "unrecognized arguments" in str(result.error)):
        fallback_argv = ["python3", str(script_path), "scan", "--pattern", pattern, "--watchlist", watchlist]
        result = await _run_argv_command(fallback_argv, ctx=ctx, cwd=str(skill_root))

    parsed = _extract_json_payload(result.output)
    parsed = _apply_scan_mode(parsed, mode, top_value)
    response = {"parsed": parsed, "raw": result.output}
    artifacts = dict(result.artifacts or {})
    artifacts.update(
        {
            "skill_id": "swing-trader",
            "skill_root": str(skill_root),
            "argv": argv,
            "fallback_argv": fallback_argv,
            "parsed": parsed,
            "raw": result.output,
        }
    )
    return ToolResult(output=json.dumps(response), error=result.error, artifacts=artifacts)


def _codex_service(ctx: ToolContext) -> CodexProposalService:
    return CodexProposalService(
        root=ctx.config.paths.root,
        store=ctx.store,
        command_runner=ctx.command_runner,
        codex_model=ctx.config.provider.codex_model,
        codex_auth_file=ctx.config.provider.codex_auth_file,
    )


async def _codex_propose(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    repo_path = str(params.get("repo_path", "")).strip()
    task = str(params.get("task", "")).strip()
    context = str(params.get("context", "")).strip()
    files_raw = params.get("files", [])
    files = [str(item).strip() for item in files_raw if str(item).strip()] if isinstance(files_raw, list) else []
    if not repo_path or not task:
        return ToolResult(output="", error="missing repo_path or task")
    background_result = _enqueue_background_tool(
        "codex_propose",
        {
            "repo_path": repo_path,
            "task": task,
            "context": context,
            "files": files,
            "background": params.get("background", False),
        },
        ctx=ctx,
    )
    if background_result is not None:
        return background_result
    try:
        result = await _codex_service(ctx).propose(
            repo_path=repo_path,
            task=task,
            context=context,
            files=files,
        )
    except Exception as error:
        return ToolResult(output="", error=f"codex proposal failed: {error}")
    return ToolResult(
        output=result["summary"],
        artifacts={
            "proposal_id": result["proposal_id"],
            "paths": result["paths"],
            "summary": result["summary"],
        },
    )


async def _codex_apply_proposal(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    proposal_id = str(params.get("proposal_id", "")).strip()
    if not proposal_id:
        return ToolResult(output="", error="missing proposal_id")
    try:
        result = await _codex_service(ctx).apply_proposal(proposal_id)
    except Exception as error:
        return ToolResult(output="", error=f"proposal apply failed: {error}")
    return ToolResult(
        output=result["message"] if "message" in result else f"Applied proposal {proposal_id}",
        artifacts=result,
    )


async def _codex_run_tests(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    proposal_id = str(params.get("proposal_id", "")).strip()
    if not proposal_id:
        return ToolResult(output="", error="missing proposal_id")
    background_result = _enqueue_background_tool(
        "codex_run_tests",
        {
            "proposal_id": proposal_id,
            "background": params.get("background", False),
        },
        ctx=ctx,
    )
    if background_result is not None:
        return background_result
    try:
        result = await _codex_service(ctx).run_tests(proposal_id)
    except Exception as error:
        return ToolResult(output="", error=f"proposal test run failed: {error}")
    lines = []
    for item in result["results"]:
        status = "PASS" if item["success"] else "FAIL"
        lines.append(f"{status}: {item['command']}")
    return ToolResult(
        output="\n".join(lines) if lines else "No test commands were run.",
        artifacts=result,
    )


# --- Reminders ---


async def _reminder_create(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    text = str(params.get("text", "")).strip()
    due = str(params.get("due", "")).strip()
    if not text or not due:
        return ToolResult(output="", error="missing text or due fields")
    try:
        datetime.fromisoformat(due)
    except ValueError:
        return ToolResult(output="", error="invalid due datetime format")
    reminder = ctx.store.create_reminder(
        adapter="repl",
        channel_id="terminal",
        user_id=ctx.user_id,
        message=text,
        due_at=due,
    )
    return ToolResult(
        output=f"Reminder scheduled: {text} at {due}",
        artifacts={"reminder_id": reminder.reminder_id},
    )


# --- Registration ---


def register_builtin_tools(registry: ToolRegistry, config: Any | None = None) -> None:
    """Register all builtin tools into the given registry."""
    fs_mutation_approval = _fs_mutation_needs_approval(config)
    tools = [
        ToolDef("memory_read", "Read user, session, or global memory.", {"type": "object", "properties": {"scope": {"type": "string", "enum": ["all", "user", "session", "global"]}}}, _memory_read, category="memory"),
        ToolDef("memory_write", "Write to user, session, or global memory.", {"type": "object", "properties": {"scope": {"type": "string"}, "content": {"type": "string"}, "name": {"type": "string"}}, "required": ["scope", "content"]}, _memory_write, category="memory"),
        ToolDef("memory_delete", "Delete a memory entry.", {"type": "object", "properties": {"scope": {"type": "string"}, "content": {"type": "string"}, "name": {"type": "string"}}, "required": ["scope", "content"]}, _memory_delete, needs_approval=True, category="memory"),
        ToolDef("memory_search", "Search across all memory stores.", {"type": "object", "properties": {"query": {"type": "string"}, "scope": {"type": "string", "enum": ["all", "user", "global", "session"]}}, "required": ["query"]}, _memory_search, category="memory"),
        ToolDef("self_describe", "Describe what Synapse is.", {"type": "object", "properties": {}}, _self_describe, category="self"),
        ToolDef("self_health", "Report health stats.", {"type": "object", "properties": {}}, _self_health, category="self"),
        ToolDef("self_capabilities", "List available capabilities.", {"type": "object", "properties": {}}, _self_capabilities, category="self"),
        ToolDef("diagnosis_report", "Run diagnosis analysis.", {"type": "object", "properties": {"window_hours": {"type": "integer"}}}, _diagnosis_report, category="self"),
        ToolDef("web_search", "Search the web.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, _web_search, category="web"),
        ToolDef("web_fetch", "Fetch a URL.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, _web_fetch, category="web"),
        ToolDef(
            "shell_exec",
            "Run a non-interactive shell command. Prefer setting cwd and passing the exact command with direct args; do not rely on shell state or prompts.",
            {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}, "background": {"type": "boolean"}}, "required": ["command"]},
            _shell_exec,
            needs_approval=_shell_needs_approval,
            category="shell",
        ),
        ToolDef(
            "shell_readonly",
            "Run a strict allowlisted read-only command without approval. Allowed: pwd, ls, whoami, git status/diff, rg, grep.",
            {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["command"]},
            _shell_readonly,
            category="shell",
        ),
        ToolDef(
            "swing_analyze",
            "Run swing-trader technical analysis for one symbol/timeframe and return parsed JSON plus raw output.",
            {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "timeframe": {"type": "string", "enum": ["1m", "5m", "15m", "1h", "4h", "daily", "weekly", "monthly"]},
                },
                "required": ["symbol"],
            },
            _swing_analyze,
            category="trading",
        ),
        ToolDef(
            "swing_scan",
            "Scan swing-trader watchlists by pattern and return parsed JSON plus raw output.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "watchlist": {"type": "string"},
                    "mode": {"type": "string", "enum": ["trade_ready", "near_setups"]},
                    "top": {"type": "integer", "minimum": 1},
                },
                "required": ["pattern"],
            },
            _swing_scan,
            category="trading",
        ),
        ToolDef("repo_status", "Read-only git repo status via `git status -sb`.", {"type": "object", "properties": {"cwd": {"type": "string"}}}, _repo_status, category="shell"),
        ToolDef("repo_diffstat", "Read-only git repo diff summary via `git diff --stat`.", {"type": "object", "properties": {"cwd": {"type": "string"}}}, _repo_diffstat, category="shell"),
        ToolDef("repo_diff", "Read-only git repo patch via `git diff`.", {"type": "object", "properties": {"cwd": {"type": "string"}}}, _repo_diff, category="shell"),
        ToolDef(
            "repo_grep",
            "Search the repository with ripgrep when available, falling back to grep.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "cwd": {"type": "string"},
                    "glob": {"type": "string"},
                    "max": {"type": "integer"},
                },
                "required": ["query"],
            },
            _repo_grep,
            category="repo",
        ),
        ToolDef(
            "repo_open",
            "Read a file from the repository root with optional line slicing.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "from": {"type": "integer"},
                    "lines": {"type": "integer"},
                },
                "required": ["path"],
            },
            _repo_open,
            category="repo",
        ),
        ToolDef(
            "fs_read",
            "Read a UTF-8 text file with optional line slicing. Paths are restricted to the repo root unless FS_ALLOW_ABSOLUTE=1.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "from": {"type": "integer"},
                    "lines": {"type": "integer"},
                },
                "required": ["path"],
            },
            _fs_read,
            category="fs",
        ),
        ToolDef(
            "fs_write",
            "Write UTF-8 text to a file. Can create parent directories when mkdirp=true.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mkdirp": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            _fs_write,
            needs_approval=fs_mutation_approval,
            category="fs",
        ),
        ToolDef(
            "fs_edit",
            "Replace one exact text match in a UTF-8 text file.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
            _fs_edit,
            needs_approval=fs_mutation_approval,
            category="fs",
        ),
        ToolDef(
            "patch_apply",
            "Apply a unified diff to the working tree with git apply after approval, and archive the patch under var/proposals/manual/<id>/.",
            {
                "type": "object",
                "properties": {
                    "patch": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                "required": ["patch"],
            },
            _patch_apply,
            needs_approval=True,
            category="coding",
        ),
        ToolDef(
            "codex_propose",
            "Generate a proposal-only Codex change bundle under var/proposals/<proposal_id>/ without editing the source repo.",
            {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string"},
                    "task": {"type": "string"},
                    "context": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "background": {"type": "boolean"},
                },
                "required": ["repo_path", "task"],
            },
            _codex_propose,
            category="coding",
        ),
        ToolDef(
            "codex_apply_proposal",
            "Apply a previously generated PATCH.diff to the working tree with git apply. Requires approval.",
            {"type": "object", "properties": {"proposal_id": {"type": "string"}}, "required": ["proposal_id"]},
            _codex_apply_proposal,
            needs_approval=True,
            category="coding",
        ),
        ToolDef(
            "codex_run_tests",
            "Run a proposal's test commands in a temporary sandbox after applying the patch there.",
            {"type": "object", "properties": {"proposal_id": {"type": "string"}, "background": {"type": "boolean"}}, "required": ["proposal_id"]},
            _codex_run_tests,
            category="coding",
        ),
        ToolDef("load_skill", "Load full SKILL.md content by skill ID.", {"type": "object", "properties": {"skill_id": {"type": "string"}}, "required": ["skill_id"]}, _load_skill, category="skills"),
        ToolDef("reminder_create", "Schedule a reminder.", {"type": "object", "properties": {"text": {"type": "string"}, "due": {"type": "string"}}, "required": ["text", "due"]}, _reminder_create, category="reminders"),
    ]
    for tool in tools:
        registry.register(tool)
