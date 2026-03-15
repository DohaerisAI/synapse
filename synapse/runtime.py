from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

from .adapters import TelegramAdapter
from .auth import AuthStore
from .streaming.telegram_stream import TelegramDraftStream
from .channels import ChannelRegistry
from .channels.telegram import TelegramPlugin
from .config import AppConfig, CONFIG_FIELDS, load_config, merged_runtime_env
from .diagnosis import DiagnosisEngine
from .executors import HostExecutor, IsolatedExecutor
from .hooks import HookRunner
from .mcp.adapter import MCPAdapter
from .mcp.registry import MCPRegistry
from .mcp.stdio_transport import StdioMcpTransport
from .mcp.transport import HttpMcpTransport
from .mcp.types import MCPAuth
from .introspection import RuntimeIntrospector
from .jobs import JobArtifacts, JobService
from .plugins import PluginRegistry, discover_plugins, load_all as load_all_plugins
from .gateway import Gateway
from .tools.registry import ToolDef, ToolRegistry, ToolResult
from .tools.builtins import register_builtin_tools
from .tools.mcp_tools import _mcp_approval_policy
from .approvals import ApprovalManager
from .gws import DEFAULT_GWS_ALLOWED_SERVICES, GWSBridge
from .identifiers import derive_session_key
from .integrations import IntegrationRegistry
from .memory import MemoryStore
from .models import ApprovalStatus, DeliveryTarget, HeartbeatStatus, InputStatus, JobRecord, JobStatus, NormalizedInboundEvent, ReminderStatus, RunState, utc_now
from .providers import ModelRouter
from .session import SessionStateMachine
from .skill_runtime import CommandRunner
from .skills import SkillHotReloader, SkillRegistry, install_skill_bundle
from .store import SQLiteStore
from .workspace import WorkspaceStore


@dataclass(slots=True)
class Runtime:
    config: AppConfig
    store: SQLiteStore
    memory: MemoryStore
    skills: SkillRegistry
    auth: AuthStore
    integrations: IntegrationRegistry
    gateway: Gateway
    telegram: TelegramAdapter
    channel_registry: ChannelRegistry
    plugin_registry: PluginRegistry
    hooks: HookRunner
    gws: GWSBridge
    workspace: WorkspaceStore
    env: dict[str, str]
    mcp_registry: MCPRegistry | None = None
    diagnosis_engine: DiagnosisEngine | None = None
    introspector: RuntimeIntrospector | None = None
    job_service: JobService | None = None
    background_services_owned: bool = False
    background_services_started: bool = False
    background_services_note: str | None = None
    _service_lock_handle: object | None = None
    heartbeat_enabled: bool = False
    heartbeat_every_minutes: int = 10
    heartbeat_target: str = "last"
    heartbeat_ack_mode: str = "silent_ok"
    heartbeat_active_hours: str = ""
    heartbeat_max_chars: int = 400
    heartbeat_next_due_at: str | None = None
    _heartbeat_thread: threading.Thread | None = None
    _heartbeat_stop_event: threading.Event | None = None
    # Anti-spam: track last delivered heartbeat message digest in-memory.
    _heartbeat_last_digest: str | None = None
    _heartbeat_last_delivered_at: str | None = None
    _skill_reload_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _skill_hot_reloader: SkillHotReloader | None = None

    def initialize(self) -> None:
        self.config.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory.initialize()
        self.workspace.initialize()
        self.store.initialize()
        self.integrations.initialize()
        self.skills.load()
        self.integrations.activate_existing()
        self.reload_skills()
        self._configure_telegram_commands()
        self._start_skill_hot_reloader()
        telegram_status = self.telegram.status_snapshot()
        self.store.upsert_adapter_health(
            adapter="telegram",
            status=telegram_status["status"],
            auth_required=self.telegram.token is None,
        )

    def shutdown(self) -> None:
        self.stop_background_services()
        self._stop_skill_hot_reloader()

    def _configure_telegram_commands(self) -> None:
        """Register a small curated Telegram command menu."""
        try:
            commands = [
                {"command": "help", "description": "Show help"},
                {"command": "usage", "description": "Usage / tokens / cost summary"},
                {"command": "memory", "description": "Show memory"},
                {"command": "skills", "description": "List skills"},
                {"command": "search", "description": "Web search"},
                {"command": "shell", "description": "Run a command"},
                {"command": "jobs", "description": "Background jobs"},
                {"command": "gws", "description": "Google Workspace"},
            ]
            self.telegram.set_commands(commands)
        except Exception:
            pass

    def reload_skills(self) -> dict[str, object]:
        with self._skill_reload_lock:
            skills = self.skills.load()
            _register_manifest_skill_tools(
                self.gateway.tool_registry,
                self.skills,
                self.mcp_registry,
            )
            tool_names: list[str] = []
            if self.gateway.tool_registry is not None:
                tool_names = sorted(
                    tool.name
                    for tool in self.gateway.tool_registry.all_tools()
                    if tool.name.startswith("skill.")
                )
            snapshot = {
                "skills": sorted(skills),
                "skill_count": len(skills),
                "tools": tool_names,
                "tool_count": len(tool_names),
            }
        logger.info(
            "skills reloaded: %d skills, %d manifest tools",
            snapshot["skill_count"],
            snapshot["tool_count"],
        )
        return snapshot

    def install_skill(self, source_path: str) -> dict[str, object]:
        with self._skill_reload_lock:
            summary = install_skill_bundle(
                root=self.config.paths.root,
                skills_dir=self.config.paths.skills_dir,
                source_path=source_path,
            )
            summary["reload"] = self.reload_skills()
            return summary

    def start_background_services(self) -> None:
        if self.background_services_started:
            return
        if not self._acquire_service_lock():
            self.background_services_started = False
            return
        self._reset_transient_state(reason="startup")
        self._connect_mcp_servers()
        self.telegram.set_handlers(
            inbound_handler=self.handle_telegram_event,
            health_handler=self.store.upsert_adapter_health,
        )
        self.telegram.start()
        if self.job_service is not None:
            self.job_service.start()
        self.background_services_owned = True
        self.background_services_started = True
        self.background_services_note = None
        self._start_heartbeat_scheduler()

    def stop_background_services(self) -> None:
        if not self.background_services_owned:
            self.background_services_started = False
            return
        self._stop_heartbeat_scheduler()
        if self.job_service is not None:
            self.job_service.stop()
        self._disconnect_mcp_servers()
        self.telegram.stop()
        self._reset_transient_state(reason="shutdown")
        self.background_services_owned = False
        self.background_services_started = False
        self._release_service_lock()

    def handle_telegram_event(self, event) -> None:
        try:
            asyncio.run(self.async_handle_telegram_event(event))
        except Exception:
            logger.exception("handle_telegram_event crashed for event %s", getattr(event, "message_id", "?"))
            # Best-effort error reply so user isn't left hanging
            try:
                channel_id = getattr(event, "channel_id", "")
                if channel_id and self.telegram.token:
                    self.telegram.send_text(channel_id, "Something went wrong processing your message. Please try again.")
            except Exception:
                logger.exception("failed to send error reply")

    async def async_handle_telegram_event(self, event) -> None:
        stream: TelegramDraftStream | None = None
        if getattr(event, "adapter", None) == "telegram" and self.telegram.token:
            meta = getattr(event, "metadata", {}) or {}
            prefer_draft = meta.get("chat_type") == "private"
            stream = TelegramDraftStream(
                self.telegram,
                getattr(event, "channel_id", ""),
                prefer_draft=bool(prefer_draft),
            )
            await stream.start()  # Typing heartbeat starts immediately
        try:
            result = await self.gateway.ingest(event, stream_sink=stream)
        except Exception:
            logger.exception("gateway.ingest failed for event %s", getattr(event, "message_id", "?"))
            if stream is not None:
                await stream.finalize()
            raise
        if stream is not None:
            await stream.materialize()
            logger.info(
                "post-materialize: streamed=%s transport=%s queued=%s reply_len=%d",
                stream.streamed, stream.transport, result.queued,
                len(result.reply_text or ""),
            )
            if stream.streamed:
                if result.queued:
                    self.deliver_result(result)
            else:
                self.deliver_result(result)
        else:
            self.deliver_result(result)

    def deliver_result(self, result) -> None:
        if result.queued or not result.reply_text or getattr(result, "suppress_delivery", False):
            return
        delivery_target = getattr(result, "delivery_target", None)
        if delivery_target is None:
            run = self.store.get_run(result.run_id)
            if run is None:
                return
            delivery_target = DeliveryTarget(
                adapter=run.adapter,
                channel_id=run.channel_id,
                user_id=run.user_id,
                message_id=getattr(result, "in_reply_to_message_id", None),
            )
        self._send_direct(
            delivery_target.adapter,
            delivery_target.channel_id,
            result.reply_text,
            raise_on_unavailable=False,
        )

        # Best-effort lightweight reactions on Telegram.
        # This is intentionally subtle (like OpenClaw): only for acknowledgements.
        if delivery_target.adapter == "telegram":
            msg_id = getattr(delivery_target, "message_id", None)
            if msg_id is None:
                msg_id = getattr(result, "in_reply_to_message_id", None)
            if msg_id is not None and isinstance(msg_id, str) and msg_id.isdigit():
                if any(result.reply_text.lower().startswith(p) for p in ("done", "fixed", "cool", "ok", "okay", "got it", "on it")):
                    try:
                        self.telegram.set_reaction(delivery_target.channel_id, int(msg_id), "👍")
                    except Exception:
                        pass

    async def execute_job_tool(
        self,
        job: JobRecord,
        artifacts: JobArtifacts,
        cancel_event: threading.Event,
    ) -> ToolResult:
        if self.gateway.tool_registry is None:
            return ToolResult(output="", error="tool registry not configured")
        tool = self.gateway.tool_registry.get(job.tool_name)
        if tool is None:
            return ToolResult(output="", error=f"unknown tool: {job.tool_name}")
        delivery_target = None
        if (
            job.delivery_target_adapter
            and job.delivery_target_channel_id
            and job.delivery_target_user_id
        ):
            delivery_target = DeliveryTarget(
                adapter=job.delivery_target_adapter,
                channel_id=job.delivery_target_channel_id,
                user_id=job.delivery_target_user_id,
            )
        tool_context = self.gateway._build_tool_context(
            None if job.parent_run_id is None else self.store.get_run(job.parent_run_id),
            approval_id=job.approval_id,
            override_session_key=job.session_key,
            override_user_id=job.delivery_target_user_id,
            override_delivery_target=delivery_target,
            job_id=job.job_id,
            cancel_event=cancel_event,
            stdout_path=str(artifacts.stdout_path),
            stderr_path=str(artifacts.stderr_path),
        )
        params = dict(job.params)
        params.pop("background", None)
        return await tool.execute(params, ctx=tool_context)

    def handle_job_terminal(
        self,
        job: JobRecord,
        result: ToolResult | None,
        artifacts: JobArtifacts,
    ) -> None:
        follow_up = self._job_follow_up_text(job, artifacts)
        event_type = {
            JobStatus.SUCCEEDED: "job.completed",
            JobStatus.FAILED: "job.failed",
            JobStatus.CANCELLED: "job.cancelled",
        }.get(job.status)
        if event_type is not None and job.parent_run_id and job.session_key:
            self.store.append_run_event(
                job.parent_run_id,
                job.session_key,
                event_type,
                {
                    "job_id": job.job_id,
                    "tool_name": job.tool_name,
                    "status": job.status.value,
                    "result_summary": job.result_summary,
                    "error": job.error,
                    "artifact_root": job.artifact_root,
                },
            )
        if job.session_key and follow_up:
            self.memory.append_transcript(
                job.session_key,
                {
                    "role": "assistant",
                    "content": follow_up,
                    "kind": "job_follow_up",
                    "job_id": job.job_id,
                },
            )
        delivery_target = None
        if (
            job.delivery_target_adapter
            and job.delivery_target_channel_id
            and job.delivery_target_user_id
        ):
            delivery_target = DeliveryTarget(
                adapter=job.delivery_target_adapter,
                channel_id=job.delivery_target_channel_id,
                user_id=job.delivery_target_user_id,
            )
        if delivery_target is None or not follow_up:
            return
        self.deliver_result(
            type("JobGatewayResult", (), {
                "queued": False,
                "reply_text": follow_up,
                "suppress_delivery": False,
                "delivery_target": delivery_target,
                "run_id": job.parent_run_id or job.job_id,
            })()
        )

    def _job_follow_up_text(self, job: JobRecord, artifacts: JobArtifacts) -> str:
        if job.status == JobStatus.SUCCEEDED:
            summary = job.result_summary or "Background job completed."
            return (
                f"Background job `{job.job_id}` completed for `{job.tool_name}`.\n"
                f"{summary}\n"
                f"Artifacts: {artifacts.summary_path}"
            )
        if job.status == JobStatus.CANCELLED:
            return f"Background job `{job.job_id}` was cancelled."
        detail = job.error or job.result_summary or "Background job failed."
        return f"Background job `{job.job_id}` failed for `{job.tool_name}`.\n{detail}"

    def tui_snapshot(self) -> dict[str, object]:
        return {
            "health": self.store.health_snapshot(),
            "auth": self.auth.health_view(),
            "skills": list(self.skills.skills.keys()),
            "runs": [run.__dict__ if hasattr(run, "__dict__") else run for run in self.store.list_runs(limit=5)],
            "approvals": [approval.__dict__ if hasattr(approval, "__dict__") else approval for approval in self.store.list_pending_approvals()],
            "telegram": self.telegram.status_snapshot(),
            "gws": self.gws.status(),
            "mcp": [info.model_dump() for info in self.mcp_registry.list_connected()] if self.mcp_registry else [],
            "workspace": self.workspace.snapshot(),
            "config": {field: self.env.get(field, "") for field in CONFIG_FIELDS},
            "agent_name": self.gateway.agent_name,
            "integrations": [item.model_dump() for item in self.integrations.list_integrations()],
            "boot_tasks": self.integrations.boot_tasks(),
            "background_services_owned": self.background_services_owned,
            "background_services_note": self.background_services_note,
            "heartbeat": self.heartbeat_snapshot(),
            "reminders": [reminder.model_dump() for reminder in self.store.list_reminders(limit=10)],
        }

    def heartbeat_snapshot(self) -> dict[str, object]:
        latest = self.store.get_latest_heartbeat()
        status = "disabled"
        if self.heartbeat_enabled:
            status = "active" if self.background_services_owned else "configured"
        return {
            "status": status,
            "enabled": self.heartbeat_enabled,
            "every_minutes": self.heartbeat_every_minutes,
            "target": self.heartbeat_target,
            "ack_mode": self.heartbeat_ack_mode,
            "active_hours": self.heartbeat_active_hours,
            "max_chars": self.heartbeat_max_chars,
            "next_due_at": self.heartbeat_next_due_at,
            "latest": None if latest is None else latest.model_dump(),
        }

    async def maybe_run_heartbeat(self, *, now: datetime | None = None) -> object | None:
        if not self.heartbeat_enabled or not self.background_services_owned:
            return None
        current = now or utc_now()
        if self.store.has_any_active_run():
            self._record_heartbeat_skip(current, "busy")
            return None
        if not self._within_active_hours(current):
            self._record_heartbeat_skip(current, "outside_active_hours")
            return None
        target = self.store.get_last_delivery_target()
        if target is None:
            self._record_heartbeat_skip(current, "no_last_channel")
            return None

        scheduled_for = current.isoformat()
        heartbeat = self.store.create_heartbeat(
            status=HeartbeatStatus.RUNNING,
            scheduled_for=scheduled_for,
            delivery_target=target,
            started_at=scheduled_for,
        )
        try:
            event = self._heartbeat_event(target, heartbeat.heartbeat_id, current)
            result = await self.gateway.ingest(event)
            reply_text = result.reply_text.strip()
            suppress_delivery = False
            if self.heartbeat_ack_mode == "store_only":
                suppress_delivery = True
            elif self.heartbeat_ack_mode == "silent_ok" and reply_text == "HEARTBEAT_OK":
                suppress_delivery = True

            # Truncate before dedupe so we compare what the user would see.
            if len(result.reply_text) > self.heartbeat_max_chars:
                result.reply_text = result.reply_text[: max(0, self.heartbeat_max_chars - 12)].rstrip() + " [truncated]"
                reply_text = result.reply_text.strip()

            # Dedupe: if the heartbeat output is identical to the last delivered heartbeat,
            # suppress delivery to avoid spam.
            if not suppress_delivery and reply_text:
                digest = hashlib.sha256(reply_text.encode("utf-8")).hexdigest()
                last_digest = self._heartbeat_last_digest or self.store.get_heartbeat_last_digest()
                if digest == last_digest:
                    suppress_delivery = True
                else:
                    self._heartbeat_last_digest = digest
                    delivered_at = utc_now().isoformat()
                    self._heartbeat_last_delivered_at = delivered_at
                    self.store.set_heartbeat_last_digest(digest, delivered_at=delivered_at)

            result.suppress_delivery = suppress_delivery
            self.deliver_result(result)
            self.store.update_heartbeat(
                heartbeat.heartbeat_id,
                status=HeartbeatStatus.COMPLETED,
                completed_at=utc_now().isoformat(),
                delivery_target=target,
                ack_suppressed=suppress_delivery,
                run_id=result.run_id,
            )
            return result
        except Exception as error:
            self.store.update_heartbeat(
                heartbeat.heartbeat_id,
                status=HeartbeatStatus.FAILED,
                completed_at=utc_now().isoformat(),
                delivery_target=target,
                last_error=str(error),
            )
            return None
        finally:
            self.heartbeat_next_due_at = (current + timedelta(minutes=self.heartbeat_every_minutes)).isoformat()

    def maybe_dispatch_due_reminders(self, *, now: datetime | None = None) -> int:
        if not self.background_services_owned:
            return 0
        current = now or utc_now()
        due = self.store.claim_due_reminders(current.isoformat())
        delivered = 0
        for reminder in due:
            try:
                self._send_direct(reminder.adapter, reminder.channel_id, reminder.message, raise_on_unavailable=True)
                self.store.update_reminder(
                    reminder.reminder_id,
                    status=ReminderStatus.DELIVERED,
                    delivered_at=current.isoformat(),
                )
                session_key = derive_session_key(reminder.adapter, reminder.channel_id, reminder.user_id)
                self.memory.append_transcript(
                    session_key,
                    {"role": "assistant", "content": reminder.message, "kind": "reminder"},
                )
                delivered += 1
            except Exception as error:
                self.store.update_reminder(
                    reminder.reminder_id,
                    status=ReminderStatus.FAILED,
                    last_error=str(error),
                )
        return delivered

    def _connect_mcp_servers(self) -> None:
        """Connect to configured MCP servers (best-effort, non-blocking).

        Detects whether we're inside a running event loop (e.g. uvicorn lifespan)
        and uses the appropriate async strategy.
        """
        if self.mcp_registry is None or not self.config.mcp.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self._async_connect_mcp_servers())
        else:
            asyncio.run(self._async_connect_mcp_servers())

    async def _async_connect_mcp_servers(self) -> None:
        """Async implementation of MCP server connection."""
        if self.mcp_registry is None:
            return
        for conn in self.config.mcp.connections:
            if not conn.enabled:
                continue
            adapter = MCPAdapter(
                server_id=conn.server_id,
                url=conn.url,
                auth=MCPAuth(
                    auth_type=conn.auth.auth_type,
                    token=conn.auth.token,
                    refresh_url=conn.auth.refresh_url,
                    scopes=list(conn.auth.scopes),
                ),
            )
            if conn.transport == "stdio":
                cmd_parts = conn.command.split() if conn.command else ["npx", "mcp-remote"]
                cmd = [*cmd_parts, conn.url]
                # Pass auth token as --header for mcp-remote (bypasses broken OAuth redirect)
                if conn.auth.token:
                    cmd.extend(["--header", f"Authorization:Bearer {conn.auth.token}"])
                transport = StdioMcpTransport(command=cmd, url=conn.url)
            else:
                transport = HttpMcpTransport(
                    url=conn.url,
                    auth_token=conn.auth.token,
                    auth_type=conn.auth.auth_type,
                )
            adapter._transport = transport
            try:
                await self.mcp_registry.register_adapter(adapter)
                self.store.upsert_mcp_connection(
                    server_id=conn.server_id,
                    url=conn.url,
                    auth_type=conn.auth.auth_type,
                    status="connected",
                )
                # Register MCP tools into the tool registry
                if self.gateway.tool_registry is not None:
                    from .tools.mcp_tools import register_mcp_server_tools
                    try:
                        count = await register_mcp_server_tools(
                            self.gateway.tool_registry, conn.server_id, adapter,
                        )
                        logger.info("MCP tools registered: %s (%d tools)", conn.server_id, count)
                    except Exception:
                        logger.warning("Failed to register MCP tools for %s", conn.server_id, exc_info=True)
                logger.info("MCP server connected: %s", conn.server_id)
            except Exception:
                logger.warning("MCP server failed to connect: %s (will retry later)", conn.server_id, exc_info=True)
                self.store.upsert_mcp_connection(
                    server_id=conn.server_id,
                    url=conn.url,
                    auth_type=conn.auth.auth_type,
                    status="error",
                )

    def _disconnect_mcp_servers(self) -> None:
        """Disconnect all MCP servers."""
        if self.mcp_registry is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self._async_disconnect_mcp_servers())
        else:
            asyncio.run(self._async_disconnect_mcp_servers())

    async def _async_disconnect_mcp_servers(self) -> None:
        """Async implementation of MCP server disconnection."""
        if self.mcp_registry is None:
            return
        for info in self.mcp_registry.list_connected():
            try:
                await self.mcp_registry.unregister(info.server_id)
            except Exception:
                logger.warning("MCP disconnect failed: %s", info.server_id, exc_info=True)

    def _acquire_service_lock(self) -> bool:
        lock_path = self.config.paths.data_dir / "services.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            self.background_services_owned = False
            self.background_services_note = "Background services are already owned by another process."
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._service_lock_handle = handle
        return True

    def _release_service_lock(self) -> None:
        handle = self._service_lock_handle
        if handle is None:
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._service_lock_handle = None

    def _start_skill_hot_reloader(self) -> None:
        reloader = self._skill_hot_reloader
        if reloader is None:
            reloader = SkillHotReloader(
                self.config.paths.skills_dir,
                reload_callback=self.reload_skills,
            )
            self._skill_hot_reloader = reloader
        reloader.start()

    def _stop_skill_hot_reloader(self) -> None:
        reloader = self._skill_hot_reloader
        if reloader is not None:
            reloader.stop()

    def _start_heartbeat_scheduler(self) -> None:
        if self._heartbeat_thread is not None:
            if self.heartbeat_enabled and self.heartbeat_next_due_at is None:
                self.heartbeat_next_due_at = (utc_now() + timedelta(minutes=self.heartbeat_every_minutes)).isoformat()
            return
        self._heartbeat_stop_event = threading.Event()
        if self.heartbeat_enabled:
            self.heartbeat_next_due_at = (utc_now() + timedelta(minutes=self.heartbeat_every_minutes)).isoformat()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="runtime-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat_scheduler(self) -> None:
        stop_event = self._heartbeat_stop_event
        thread = self._heartbeat_thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join(timeout=2.0)
        self._heartbeat_thread = None
        self._heartbeat_stop_event = None
        self.heartbeat_next_due_at = None

    def _heartbeat_loop(self) -> None:
        stop_event = self._heartbeat_stop_event
        if stop_event is None:
            return
        while not stop_event.is_set():
            now = utc_now()
            self.maybe_dispatch_due_reminders(now=now)
            due_at = None if self.heartbeat_next_due_at is None else datetime.fromisoformat(self.heartbeat_next_due_at)
            if due_at is not None and now >= due_at:
                asyncio.run(self.maybe_run_heartbeat(now=now))
            stop_event.wait(1.0)

    def _send_direct(self, adapter: str, channel_id: str, text: str, *, raise_on_unavailable: bool) -> bool:
        if adapter == "telegram" and self.telegram.token:
            self.telegram.send_text(channel_id, text)
            self.store.upsert_adapter_health(
                adapter="telegram",
                status="healthy",
                auth_required=False,
                last_outbound_at=utc_now().isoformat(),
            )
            return True
        if raise_on_unavailable:
            raise RuntimeError(f"adapter unavailable for reminder delivery: {adapter}")
        return False

    def _heartbeat_event(self, target: DeliveryTarget, heartbeat_id: str, now: datetime) -> NormalizedInboundEvent:
        return NormalizedInboundEvent(
            adapter=target.adapter,
            channel_id=target.channel_id,
            user_id=target.user_id,
            message_id=f"heartbeat-{heartbeat_id}",
            text="Perform a proactive heartbeat review. Return HEARTBEAT_OK if nothing needs attention; otherwise return one concise actionable message.",
            occurred_at=now,
            metadata={"kind": "heartbeat", "heartbeat_id": heartbeat_id, "target": self.heartbeat_target},
        )

    def _record_heartbeat_skip(self, now: datetime, reason: str) -> None:
        target = self.store.get_last_delivery_target()
        self.store.create_heartbeat(
            status=HeartbeatStatus.SKIPPED,
            scheduled_for=now.isoformat(),
            delivery_target=target,
            skip_reason=reason,
            completed_at=now.isoformat(),
        )
        self.heartbeat_next_due_at = (now + timedelta(minutes=self.heartbeat_every_minutes)).isoformat()

    def _within_active_hours(self, current: datetime) -> bool:
        window = self.heartbeat_active_hours.strip()
        if not window:
            return True
        if "-" not in window:
            return True
        start_raw, end_raw = [item.strip() for item in window.split("-", 1)]
        try:
            start_hour, start_minute = [int(item) for item in start_raw.split(":", 1)]
            end_hour, end_minute = [int(item) for item in end_raw.split(":", 1)]
        except ValueError:
            return True
        current_minutes = current.hour * 60 + current.minute
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes <= end_minutes
        return current_minutes >= start_minutes or current_minutes <= end_minutes

    def _reset_transient_state(self, *, reason: str) -> None:
        cleared = self.store.clear_queued_events()
        pending_by_run = {}
        for approval in self.store.list_pending_approvals():
            pending_by_run.setdefault(approval.run_id, []).append(approval)
        inputs_by_run = {}
        for input_request in self.store.list_pending_inputs():
            inputs_by_run.setdefault(input_request.run_id, []).append(input_request)
        for run in self.store.list_active_runs():
            for approval in pending_by_run.get(run.run_id, []):
                self.store.update_approval_status(approval.approval_id, ApprovalStatus.REJECTED)
            for input_request in inputs_by_run.get(run.run_id, []):
                self.store.update_input_request(input_request.input_id, status=InputStatus.CANCELLED)
            self.store.set_run_state(run.run_id, RunState.CANCELLED)
            self.store.append_run_event(
                run.run_id,
                run.session_key,
                "run.runtime_reset",
                {"reason": reason, "cleared_queued_events": cleared},
            )


def _make_lazy_mcp_tool_fn(mcp_registry: MCPRegistry, server_id: str, tool_name: str):
    """Create a lazy MCP tool function that looks up the adapter at call time."""

    async def _execute(params, *, ctx=None):
        adapter = mcp_registry.get(server_id)
        if adapter is None:
            return ToolResult(output="", error=f"MCP server '{server_id}' not connected")
        result = await adapter.call_tool(tool_name, params)
        if not result.success:
            return ToolResult(output="", error=result.error or f"{tool_name} failed")
        output = json.dumps(result.data, indent=2, default=str) if result.data is not None else "ok"
        return ToolResult(output=output, artifacts={"latency_ms": result.latency_ms})

    return _execute


def build_runtime(root: Path | None = None) -> Runtime:
    resolved_root = root if root is not None else Path.cwd()
    env = merged_runtime_env(resolved_root, dict(os.environ))
    config = load_config(resolved_root, env)
    memory = MemoryStore(config.paths.memory_dir)
    workspace = WorkspaceStore(config.paths.root, memory)
    skills = SkillRegistry(config.paths.skills_dir)
    auth = AuthStore(
        config.paths.auth_config_path,
        config.paths.fallback_config_path,
        env=env,
    )
    integrations = IntegrationRegistry(
        config.paths.integrations_dir,
        skills_dir=config.paths.skills_dir,
        boot_path=config.paths.root / "BOOT.md",
        env=env,
    )
    store = SQLiteStore(config.paths.sqlite_path)
    hooks = HookRunner()
    model_router = ModelRouter(auth, workdir=str(config.paths.root), store=store)
    allowed_services = {
        item.strip().lower()
        for item in config.gws.allowed_services.split(",")
        if item.strip()
    }
    gws = GWSBridge(
        enabled=config.gws.enabled,
        binary=config.gws.binary,
        allowed_services=allowed_services or set(DEFAULT_GWS_ALLOWED_SERVICES.split(",")),
        env=env,
        workdir=str(config.paths.root),
    )
    telegram = TelegramAdapter(
        token=config.telegram.bot_token or None,
        polling_enabled=config.telegram.polling_enabled,
        poll_interval=config.telegram.poll_interval,
        reactions_enabled=bool(getattr(config.telegram, "reactions_enabled", True)),
    )
    channel_registry = ChannelRegistry()
    channel_registry.register(TelegramPlugin.create(telegram))
    plugin_registry = PluginRegistry()
    plugin_search_paths = [
        config.paths.root / "plugins",
        config.paths.root / "extensions",
    ]
    discovered = discover_plugins(*plugin_search_paths)
    load_all_plugins(discovered, plugin_registry)
    introspector = RuntimeIntrospector(
        plugin_registry=plugin_registry,
        skill_registry=skills,
    )
    diagnosis_engine = DiagnosisEngine(store=store)
    mcp_registry: MCPRegistry | None = None
    if config.mcp.enabled:
        mcp_registry = MCPRegistry()
    # Build tool registry (ReAct loop path)
    tool_registry = ToolRegistry()
    register_builtin_tools(tool_registry, config=config)
    approval_path = config.paths.data_dir / "approvals.json"
    approval_manager = ApprovalManager(approval_path)
    command_runner = CommandRunner(config=config, skill_registry=skills)

    gateway = Gateway(
        store=store,
        memory=memory,
        workspace=workspace,
        skills=skills,
        state_machine=SessionStateMachine(),
        host_executor=HostExecutor(
            memory,
            skills,
            store,
            integrations,
            gws,
            codex_model=config.provider.codex_model,
            workdir=str(config.paths.root),
            introspector=introspector,
            diagnosis_engine=diagnosis_engine,
            command_runner=command_runner,
            pricing=config.pricing,
        ),
        isolated_executor=IsolatedExecutor(command_runner=command_runner),
        model_router=model_router,
        agent_name=config.agent.name,
        assistant_instructions=config.agent.extra_instructions,
        heartbeat_path=config.paths.root / "HEARTBEAT.md",
        hooks=hooks,
        tool_registry=tool_registry,
        approval_manager=approval_manager,
        diagnosis_engine=diagnosis_engine,
        command_runner=command_runner,
    )
    gateway._config = config
    runtime = Runtime(
        config=config,
        store=store,
        memory=memory,
        skills=skills,
        auth=auth,
        integrations=integrations,
        gateway=gateway,
        telegram=telegram,
        channel_registry=channel_registry,
        plugin_registry=plugin_registry,
        hooks=hooks,
        gws=gws,
        workspace=workspace,
        mcp_registry=mcp_registry,
        diagnosis_engine=diagnosis_engine,
        introspector=introspector,
        env=env,
        heartbeat_enabled=config.heartbeat.enabled,
        heartbeat_every_minutes=config.heartbeat.every_minutes,
        heartbeat_target=config.heartbeat.target,
        heartbeat_ack_mode=config.heartbeat.ack_mode,
        heartbeat_active_hours=config.heartbeat.active_hours,
        heartbeat_max_chars=config.heartbeat.max_chars,
    )
    runtime.initialize()
    runtime.job_service = JobService(
        store=store,
        root=config.paths.root / "var" / "jobs",
        execute_job=runtime.execute_job_tool,
        on_terminal=runtime.handle_job_terminal,
        concurrency=config.jobs.max_concurrency,
    )
    gateway._job_service = runtime.job_service
    return runtime


def _register_manifest_skill_tools(
    tool_registry: ToolRegistry | None,
    skills: SkillRegistry,
    mcp_registry: MCPRegistry | None,
) -> None:
    if tool_registry is None:
        return
    replacements: list[ToolDef] = []
    for skill_id in skills.skills:
        skill_tools = skills.get_skill_tools(skill_id)
        if not skill_tools:
            continue
        for tool_spec in skill_tools:
            mcp_ref = tool_spec.get("mcp", "")
            if mcp_ref and mcp_registry is not None:
                parts = mcp_ref.split(".", 1)
                if len(parts) == 2:
                    srv_id, mcp_tool_name = parts
                    execute_fn = _make_lazy_mcp_tool_fn(mcp_registry, srv_id, mcp_tool_name)
                    replacements.append(
                        ToolDef(
                            name=f"skill.{skill_id}.{tool_spec['name']}",
                            description=tool_spec.get("description", ""),
                            input_schema=tool_spec.get("parameters", {}),
                            execute=execute_fn,
                            needs_approval=_mcp_approval_policy(srv_id, mcp_tool_name),
                            category=f"skill.{skill_id}",
                        )
                    )
                    continue
            _sid, _name = skill_id, tool_spec.get("name", "")

            async def _skill_hint(params, *, ctx=None, sid=_sid, name=_name):
                return ToolResult(
                    output=f"Skill tool '{sid}.{name}' requires loading the skill first. Use load_skill."
                )

            replacements.append(
                ToolDef(
                    name=f"skill.{skill_id}.{tool_spec['name']}",
                    description=tool_spec.get("description", ""),
                    input_schema=tool_spec.get("parameters", {}),
                    execute=_skill_hint,
                    category=f"skill.{skill_id}",
                )
            )
    tool_registry.replace_tools(
        predicate=lambda tool: tool.name.startswith("skill."),
        replacements=replacements,
    )
