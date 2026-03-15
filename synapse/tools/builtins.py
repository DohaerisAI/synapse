"""Builtin tools — memory, self-awareness, web, shell, skills, reminders."""
from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
import re

import httpx

from .registry import ToolContext, ToolDef, ToolRegistry, ToolResult

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = re.compile(
    r"^(localhost|127\.|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|169\.254\.|0\.|::1|fc00:|fe80:|fd)",
    re.IGNORECASE,
)
_SAFE_SHELL_COMMANDS = {"pwd", "ls", "whoami"}
_GWS_WRITE_KEYWORDS = {"send", "create", "delete", "remove", "insert", "update", "patch", "trash"}


def _shell_needs_approval(params: dict[str, Any]) -> bool:
    command = str(params.get("command", "")).strip()
    first = command.split(" ", 1)[0]
    if first in _SAFE_SHELL_COMMANDS:
        return False
    if first == "gws":
        # GWS read commands are safe; write commands need approval
        return any(kw in command.lower() for kw in _GWS_WRITE_KEYWORDS)
    return True


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


async def _shell_exec(params: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
    command = str(params.get("command", "")).strip()
    if not command:
        return ToolResult(output="", error="missing command")
    # Rewrite bare python3/python to use the venv interpreter
    parts = shlex.split(command)
    if parts and parts[0] in ("python3", "python"):
        import sys
        parts[0] = sys.executable
    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        out = stdout.decode() if stdout else ""
        err = stderr.decode() if stderr else ""
        detail = out.strip() or err.strip() or f"exit code {proc.returncode}"
        if proc.returncode != 0:
            return ToolResult(output=detail, error=f"exit code {proc.returncode}")
        return ToolResult(output=detail)
    except Exception as e:
        return ToolResult(output="", error=f"shell exec failed: {e}")


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


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register all builtin tools into the given registry."""
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
        ToolDef("shell_exec", "Run a shell command.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, _shell_exec, needs_approval=_shell_needs_approval, category="shell"),
        ToolDef("load_skill", "Load full SKILL.md content by skill ID.", {"type": "object", "properties": {"skill_id": {"type": "string"}}, "required": ["skill_id"]}, _load_skill, category="skills"),
        ToolDef("reminder_create", "Schedule a reminder.", {"type": "object", "properties": {"text": {"type": "string"}, "due": {"type": "string"}}, "required": ["text", "due"]}, _reminder_create, category="reminders"),
    ]
    for tool in tools:
        registry.register(tool)
