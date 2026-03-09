from __future__ import annotations

import asyncio
import re
import shlex
import tempfile

from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .capabilities import DEFAULT_CAPABILITY_REGISTRY
from .diagnosis import DiagnosisEngine
from .gws import GWSBridge
from .integrations import IntegrationRegistry
from .introspection import RuntimeIntrospector
from .memory import MemoryStore
from .models import ExecutionResult, IntegrationStatus, PlannedAction, RunState
from .self_model import Identity
from .skills import SkillRegistry
from .store import SQLiteStore

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = re.compile(
    r"^(localhost|127\.|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|169\.254\.|0\.|::1|fc00:|fe80:|fd)",
    re.IGNORECASE,
)


def _validate_fetch_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"disallowed scheme: {parsed.scheme!r}")
    host = parsed.hostname or ""
    if _BLOCKED_HOSTS.match(host):
        raise ValueError(f"disallowed host: {host!r}")


class HostExecutor:
    def __init__(
        self,
        memory: MemoryStore,
        skills: SkillRegistry,
        store: SQLiteStore,
        integrations: IntegrationRegistry,
        gws: GWSBridge,
        *,
        codex_model: str = "gpt-5.4",
        workdir: str = ".",
        codex_search_runner: Callable[[str], dict[str, Any]] | None = None,
        introspector: RuntimeIntrospector | None = None,
        diagnosis_engine: DiagnosisEngine | None = None,
    ) -> None:
        self.memory = memory
        self.skills = skills
        self.store = store
        self.integrations = integrations
        self.gws = gws
        self.codex_model = codex_model
        self.workdir = workdir
        self.codex_search_runner = codex_search_runner
        self.introspector = introspector
        self.diagnosis_engine = diagnosis_engine

    async def execute(self, action: PlannedAction, *, session_key: str, user_id: str) -> ExecutionResult:
        if action.action == "memory.read":
            scope = str(action.payload.get("scope", "all"))
            artifacts: dict[str, str] = {}
            if scope in {"all", "user"}:
                artifacts["user_memory"] = self.memory.read_user_memory(user_id)
            if scope in {"all", "session"}:
                artifacts["session_notes"] = self.memory.read_session_notes(session_key)
                artifacts["session_summary"] = self.memory.read_session_summary(session_key)
                artifacts["recent_transcript"] = "\n".join(
                    f"{entry.get('role', 'unknown')}: {entry.get('content', '')}"
                    for entry in self.memory.read_recent_transcript(session_key)
                ).strip()
            if scope in {"all", "global"}:
                artifacts["global_memory"] = self.memory.read_global_memory()
            return ExecutionResult(action=action.action, success=True, detail="memory snapshot loaded", artifacts=artifacts)
        if action.action == "memory.write":
            scope = action.payload.get("scope", "session")
            content = str(action.payload.get("content", "")).strip()
            if scope == "session":
                self.memory.append_notes(session_key, content)
                return ExecutionResult(action=action.action, success=True, detail="session memory updated")
            if scope == "user":
                self.memory.append_user_memory(user_id, content)
                return ExecutionResult(action=action.action, success=True, detail="user memory updated")
            if scope == "global":
                self.memory.append_global_memory(content, name=str(action.payload.get("name", "general")))
                return ExecutionResult(action=action.action, success=True, detail="global memory updated")
        if action.action == "memory.delete":
            scope = action.payload.get("scope", "session")
            content = str(action.payload.get("content", "")).strip()
            if scope == "session":
                removed = self.memory.delete_session_notes(session_key, content)
                return ExecutionResult(action=action.action, success=removed, detail= "session memory removed" if removed else "session memory item not found")
            if scope == "user":
                removed = self.memory.delete_user_memory(user_id, content)
                return ExecutionResult(action=action.action, success=removed, detail= "user memory removed" if removed else "user memory item not found")
            if scope == "global":
                removed = self.memory.delete_global_memory(content, name=str(action.payload.get("name", "general")))
                return ExecutionResult(action=action.action, success=removed, detail= "global memory removed" if removed else "global memory item not found")
        if action.action == "telegram.send":
            return ExecutionResult(action=action.action, success=True, detail="adapter send accepted")
        if action.action == "skills.read":
            skill_ids = action.payload.get("skill_ids")
            selected_ids = [str(item) for item in skill_ids] if isinstance(skill_ids, list) else None
            return ExecutionResult(action=action.action, success=True, detail="skills loaded", artifacts={"context": self.skills.read(selected_ids or list(self.skills.skills))},
            )
        if action.action == "integration.propose":
            request = str(action.payload.get("request", "")).strip()
            record = self.integrations.propose(request)
            return ExecutionResult(action=action.action, success=True, detail=f"integration proposed: {record.integration_id}", artifacts={"integration": record.model_dump()},
            )
        if action.action == "integration.scaffold":
            integration_id = str(action.payload.get("integration_id", "")).strip()
            record = self.integrations.scaffold(integration_id)
            return ExecutionResult(action=action.action, success=True, detail=f"integration scaffolded: {record.integration_id}", artifacts={"integration": record.model_dump()},
            )
        if action.action == "integration.test":
            integration_id = str(action.payload.get("integration_id", "")).strip()
            record = self.integrations.test(integration_id)
            success = record.status is not IntegrationStatus.FAILED
            return ExecutionResult(action=action.action, success=success, detail=f"integration test {'passed' if success else 'failed'}: {record.integration_id}", artifacts={"integration": record.model_dump()},
            )
        if action.action == "integration.apply":
            integration_id = str(action.payload.get("integration_id", "")).strip()
            record = self.integrations.apply(integration_id)
            self.skills.load()
            return ExecutionResult(action=action.action, success=True, detail=f"integration applied: {record.integration_id}", artifacts={"integration": record.model_dump()},
            )
        if action.action == "capabilities.read":
            return ExecutionResult(action=action.action, success=True, detail="capabilities loaded", artifacts={"summary": DEFAULT_CAPABILITY_REGISTRY.user_bundle()},
            )
        if action.action == "web.search":
            query = str(action.payload.get("query", "")).strip()
            if not query:
                return ExecutionResult(action=action.action, success=False, detail="missing search query")
            try:
                artifacts = await self._run_codex_web_search(query)
            except Exception as error:
                return ExecutionResult(action=action.action, success=False, detail=f"web search failed: {error}")
            return ExecutionResult(action=action.action, success=True, detail=f"web search completed for: {query}", artifacts=artifacts,
            )
        if action.action.startswith("gws."):
            try:
                success, detail, artifacts = await self.gws.execute(action.action, action.payload)
            except Exception as error:
                return ExecutionResult(action=action.action, success=False, detail=f"gws action failed: {error}")
            return ExecutionResult(action=action.action, success=success, detail=detail, artifacts=artifacts)
        if action.action == "reminder.create":
            message = str(action.payload.get("message", "")).strip()
            due_at = str(action.payload.get("due_at", "")).strip()
            adapter = str(action.payload.get("adapter", "")).strip()
            channel_id = str(action.payload.get("channel_id", "")).strip()
            if not all((message, due_at, adapter, channel_id)):
                return ExecutionResult(action=action.action, success=False, detail="missing reminder fields")
            try:
                datetime.fromisoformat(due_at)
            except ValueError:
                return ExecutionResult(action=action.action, success=False, detail="invalid reminder due_at")
            reminder = self.store.create_reminder(
                adapter=adapter,
                channel_id=channel_id,
                user_id=user_id,
                message=message,
                due_at=due_at,
            )
            return ExecutionResult(action=action.action, success=True, detail="reminder scheduled", artifacts={"reminder_id": reminder.reminder_id, "due_at": due_at, "message": message},
            )
        if action.action == "self.describe":
            return self._handle_self_describe()
        if action.action == "self.health":
            return self._handle_self_health()
        if action.action == "self.capabilities":
            return self._handle_self_capabilities()
        if action.action == "self.gaps":
            return self._handle_self_gaps()
        if action.action == "diagnosis.report":
            return self._handle_diagnosis_report(action.payload)
        return ExecutionResult(action=action.action, success=False, detail="unsupported host action")

    def _handle_self_describe(self) -> ExecutionResult:
        identity = Identity(
            name="Synapse",
            version="0.1.0",
            purpose="Async Python agent runtime with explicit state machines and approval gates",
            personality="Calm, practical, concise",
        )
        architecture = self.introspector.build_architecture() if self.introspector else None
        limitations = self.introspector.discover_limitations() if self.introspector else []
        artifacts: dict[str, Any] = {
            "identity": identity.model_dump(),
        }
        if architecture:
            artifacts["architecture"] = [c.model_dump() for c in architecture.components]
        if limitations:
            artifacts["limitations"] = [lim.model_dump() for lim in limitations]
        return ExecutionResult(
            action="self.describe",
            success=True,
            detail=f"I am {identity.name}: {identity.purpose}",
            artifacts=artifacts,
        )

    def _handle_self_health(self) -> ExecutionResult:
        runs = self.store.list_runs(limit=200)
        state_counts: dict[str, int] = {}
        for run in runs:
            state_counts[run.state.value] = state_counts.get(run.state.value, 0) + 1
        total = len(runs)
        completed = state_counts.get(RunState.COMPLETED.value, 0)
        failed = state_counts.get(RunState.FAILED.value, 0)
        failure_rate = failed / total if total > 0 else 0.0
        health: dict[str, Any] = {
            "total_runs": total,
            "completed": completed,
            "failed": failed,
            "failure_rate": round(failure_rate, 3),
            "runs_by_state": state_counts,
        }
        return ExecutionResult(
            action="self.health",
            success=True,
            detail=f"Health: {total} runs, {completed} completed, {failed} failed",
            artifacts={"health": health},
        )

    def _handle_self_capabilities(self) -> ExecutionResult:
        capabilities = self.introspector.discover_capabilities() if self.introspector else []
        skills = self.introspector.discover_skills() if self.introspector else []
        plugins = self.introspector.discover_plugins() if self.introspector else []
        return ExecutionResult(
            action="self.capabilities",
            success=True,
            detail=f"{len(capabilities)} capabilities, {len(skills)} skills, {len(plugins)} plugins",
            artifacts={
                "capabilities": capabilities,
                "skills": skills,
                "plugins": plugins,
            },
        )

    def _handle_self_gaps(self) -> ExecutionResult:
        limitations = self.introspector.discover_limitations() if self.introspector else []
        return ExecutionResult(
            action="self.gaps",
            success=True,
            detail=f"{len(limitations)} known limitations",
            artifacts={"limitations": [lim.model_dump() for lim in limitations]},
        )

    def _handle_diagnosis_report(self, payload: dict[str, Any]) -> ExecutionResult:
        if not self.diagnosis_engine:
            return ExecutionResult(
                action="diagnosis.report",
                success=False,
                detail="diagnosis engine not configured",
            )
        window_hours = int(payload.get("window_hours", 24))
        report = self.diagnosis_engine.analyze_runs(window_hours=window_hours)
        return ExecutionResult(
            action="diagnosis.report",
            success=True,
            detail=f"Diagnosis: {report.total_runs} runs, health score {report.health_score:.2f}",
            artifacts={"report": report.to_dict()},
        )

    async def _run_codex_web_search(self, query: str) -> dict[str, Any]:
        if self.codex_search_runner is not None:
            return self.codex_search_runner(query)
        prompt = "\n".join(
            [
                "Answer the user's request using live web research.",
                "Use web search when needed.",
                "Return a concise answer with short source citations.",
                "Do not say you lack access if web search is possible.",
                "",
                f"Query: {query}",
            ]
        )
        with tempfile.NamedTemporaryFile(mode="r+", encoding="utf-8", suffix=".txt") as output_file:
            command = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "-C",
                self.workdir,
                "-m",
                self.codex_model,
                "-o",
                output_file.name,
                prompt,
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
            if process.returncode != 0:
                detail = stderr.decode().strip() or stdout.decode().strip() or f"exit code {process.returncode}"
                raise RuntimeError(detail)
            output_file.seek(0)
            answer = output_file.read().strip()
        if not answer:
            raise RuntimeError("codex web search returned an empty response")
        return {"query": query, "answer": answer}


class IsolatedExecutor:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=15.0)

    async def execute(self, action: PlannedAction) -> ExecutionResult:
        if action.action == "shell.exec":
            command = str(action.payload.get("command", "")).strip()
            if not command:
                return ExecutionResult(action=action.action, success=False, detail="missing shell command")
            process = await asyncio.create_subprocess_exec(
                *shlex.split(command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
            stdout_text = stdout.decode() if stdout else ""
            stderr_text = stderr.decode() if stderr else ""
            detail = stdout_text.strip() or stderr_text.strip() or f"exit code {process.returncode}"
            return ExecutionResult(
                action=action.action,
                success=process.returncode == 0,
                detail=detail,
                artifacts={"mode": "host-fallback-isolated", "exit_code": process.returncode},
            )
        if action.action == "web.fetch":
            url = str(action.payload.get("url", "")).strip()
            _validate_fetch_url(url)
            response = await self.client.get(url)
            snippet = response.text[:500]
            return ExecutionResult(
                action=action.action,
                success=response.is_success,
                detail=f"fetched {url} with status {response.status_code}",
                artifacts={"mode": "host-fallback-isolated", "body_preview": snippet},
            )
        if action.action == "code.patch.propose":
            instructions = str(action.payload.get("instructions", "")).strip()
            return ExecutionResult(action=action.action, success=True, detail="patch proposal generated", artifacts={"mode": "proposal-only", "instructions": instructions},
            )
        return ExecutionResult(action=action.action, success=False, detail="unsupported isolated action")
