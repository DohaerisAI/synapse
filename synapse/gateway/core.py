from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..executors import HostExecutor, IsolatedExecutor
from ..hooks import HookEventType, HookRunner
from ..identifiers import derive_session_key
from ..memory import MemoryStore
from ..models import (
    ApprovalStatus,
    GatewayResult,
    NormalizedInboundEvent,
    PlannedAction,
    RunRecord,
    RunState,
    WorkflowPlan,
    WorkflowStep,
)
from ..providers import ModelRouter
from ..operator import OperatorLayer
from ..session import SessionStateMachine
from ..streaming.sink import StreamSink
from ..skills import SkillRegistry
from ..store import SQLiteStore
from ..workspace import WorkspaceStore

from .context import ContextBuilder
from .extractors import RequestExtractors
from .ingest import IngestHandler
from .planner import WorkflowPlanner
from .state import StateManager

from typing import TYPE_CHECKING as _TC
if _TC:
    from ..tools.registry import ToolRegistry as _ToolRegistry
    from ..approvals import ApprovalManager as _ApprovalManager


class Gateway:
    AGENT_LOOP_MAX_TURNS = 4

    _PlannedAction = PlannedAction

    def __init__(
        self,
        *,
        store: SQLiteStore,
        memory: MemoryStore,
        workspace: WorkspaceStore,
        skills: SkillRegistry,
        state_machine: SessionStateMachine,
        model_router: ModelRouter,
        agent_name: str = "Agent",
        assistant_instructions: str = "",
        heartbeat_path: Path | None = None,
        hooks: HookRunner | None = None,
        tool_registry: "_ToolRegistry | None" = None,
        approval_manager: "_ApprovalManager | None" = None,
        diagnosis_engine: Any = None,
        command_runner: Any = None,
        operator_layer: OperatorLayer | None = None,
        # Legacy kwargs accepted but ignored (for backward compat during migration)
        broker: Any = None,
        host_executor: Any = None,
        isolated_executor: Any = None,
        gws_planner_instructions: str = "",
    ) -> None:
        self.store = store
        self.memory = memory
        self.workspace = workspace
        self.skills = skills
        self.state_machine = state_machine
        self.model_router = model_router
        self.agent_name = agent_name.strip() or "Agent"
        self.assistant_instructions = assistant_instructions.strip()
        self.gws_planner_instructions = gws_planner_instructions.strip()
        self.heartbeat_path = heartbeat_path
        self.hooks = hooks or HookRunner()
        self.tool_registry = tool_registry
        self.approval_manager = approval_manager
        self.diagnosis_engine = diagnosis_engine
        self.command_runner = command_runner
        self.operator_layer = operator_layer or OperatorLayer()

        # Legacy references kept for tests / ingest handler that still reference them
        self.broker = broker
        self.host_executor = host_executor
        self.isolated_executor = isolated_executor

        # Sub-handlers
        self.context_builder = ContextBuilder(self)
        self.extractors = RequestExtractors(self)
        self.state_manager = StateManager(self)
        self.planner = WorkflowPlanner(self)
        self.ingest_handler = IngestHandler(self)

    async def ingest(self, event: NormalizedInboundEvent, *, stream_sink: StreamSink | None = None) -> GatewayResult:
        session_key = derive_session_key(event.adapter, event.channel_id, event.user_id)
        await self.hooks.fire(HookEventType.MESSAGE_RECEIVED, {"event": event.to_dict(), "session_key": session_key})
        self.store.upsert_adapter_health(
            adapter=event.adapter,
            status="healthy",
            auth_required=False,
            last_inbound_at=event.occurred_at.isoformat(),
        )
        active_run = self.store.get_active_run(session_key)
        if active_run is not None:
            input_resolution = await self.ingest_handler.resolve_pending_input(active_run, event)
            if input_resolution is not None:
                return input_resolution
            approval_resolution = await self.ingest_handler.resolve_chat_approval(active_run, event)
            if approval_resolution is not None:
                return approval_resolution
            self.store.enqueue_event(session_key, event)
            self.memory.append_transcript(
                session_key,
                {"role": "user", "content": event.text, "queued": True, "message_id": event.message_id},
            )
            return GatewayResult(
                run_id=active_run.run_id,
                session_key=session_key,
                status="QUEUED",
                reply_text="A run is already active for this session; the new message was queued.",
                queued=True,
            )

        run = self.store.create_run(session_key, event)
        self.store.append_run_event(run.run_id, session_key, "run.received", event.to_dict())
        self.memory.append_transcript(
            session_key,
            {"role": "user", "content": event.text, "message_id": event.message_id},
        )

        current = RunState.RECEIVED
        current = self.state_manager.transition(
            run,
            current,
            RunState.CONTEXT_BUILT,
            {"skills": list(self.skills.skills), "provider": self._resolved_provider_name()},
        )

        try:
            result = await self._process_run(run, event, session_key, current, stream_sink=stream_sink)
            # Preserve the inbound message id for potential reactions.
            try:
                setattr(result, "in_reply_to_message_id", event.message_id)
            except Exception:
                pass
            return result
        except Exception:
            try:
                self.state_manager.transition(run, current, RunState.FAILED, {"detail": "unhandled exception in ingest pipeline"})
            except Exception:
                self.store.set_run_state(run.run_id, RunState.FAILED)
            raise

    async def _process_run(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        session_key: str,
        current: RunState,
        *,
        stream_sink: StreamSink | None = None,
    ) -> GatewayResult:
        # Deterministic commands (slash commands, patterns) use the planner path
        if self._is_deterministic_command(event.text):
            return await self._process_slash_command(run, event, session_key, current, stream_sink=stream_sink)

        # Everything else goes through the ReAct loop
        return await self._process_run_react(run, event, session_key, current, stream_sink=stream_sink)

    async def _process_slash_command(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        session_key: str,
        current: RunState,
        *,
        stream_sink: StreamSink | None = None,
    ) -> GatewayResult:
        """Handle deterministic slash commands via the planner → executor path."""
        workflow = await self.planner.plan_workflow(event, session_key=session_key)
        workflow, operator_notes = self.operator_layer.apply(run, event, workflow, self.tool_registry)
        for note in operator_notes:
            self.store.append_run_event(
                run.run_id,
                session_key,
                "operator.note",
                {"note": note, "phase": "workflow.apply"},
            )
        self.store.append_run_event(run.run_id, session_key, "workflow.planned", workflow.to_dict())
        current = self.state_manager.transition(
            run, current, RunState.PLANNED,
            {"workflow_id": workflow.workflow_id, "step_count": len(workflow.steps), "intent": workflow.intent},
        )

        if not workflow.steps:
            # No actions — fall through to react loop for a chat response
            return await self._process_run_react(run, event, session_key, current, stream_sink=stream_sink)

        # Execute the workflow steps
        current, execution_results = await self._execute_workflow(run, current, workflow)
        reply_text = await self.state_manager.finalize_reply(run, event, current, execution_results, workflow)
        final_current = self.state_manager.finalize_reply_text(run, event, current, execution_results, workflow, reply_text)
        await self._drain_queue(session_key)
        return GatewayResult(
            run_id=run.run_id,
            session_key=session_key,
            status=final_current.value,
            reply_text=reply_text,
        )

    async def _execute_workflow(
        self,
        run: RunRecord,
        current: RunState,
        workflow: WorkflowPlan,
    ) -> tuple[RunState, list[dict[str, Any]]]:
        """Execute workflow steps using host/isolated executors."""
        execution_results: list[dict[str, Any]] = []
        if not workflow.steps:
            return current, execution_results
        current = self.state_manager.transition(
            run, current, RunState.EXECUTING,
            {"workflow_id": workflow.workflow_id, "step_count": len(workflow.steps), "intent": workflow.intent},
        )
        for index, step in enumerate(workflow.steps, start=1):
            action = self._resolve_step_action(step.action, execution_results)
            self.store.append_run_event(
                run.run_id, run.session_key, "workflow.step.started",
                {"workflow_id": workflow.workflow_id, "step_id": step.step_id, "index": index, "action": action.to_dict()},
            )
            if self.host_executor is not None:
                result = await self.host_executor.execute(action, session_key=run.session_key, user_id=run.user_id)
            elif self.isolated_executor is not None:
                result = await self.isolated_executor.execute(action)
            else:
                from ..models import ExecutionResult
                result = ExecutionResult(action=action.action, success=False, detail="no executor available")
            result_payload = {"action": result.action, "success": result.success, "detail": result.detail, "artifacts": result.artifacts}
            execution_results.append(result_payload)
            self.store.append_run_event(
                run.run_id, run.session_key, "workflow.step.completed",
                {"workflow_id": workflow.workflow_id, "step_id": step.step_id, "index": index, "result": result_payload},
            )
            if not result.success:
                break
        current = self.state_manager.transition(run, current, RunState.VERIFYING, {"results": execution_results})
        return current, execution_results

    async def _process_run_react(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        session_key: str,
        current: RunState,
        *,
        stream_sink: StreamSink | None = None,
    ) -> GatewayResult:
        """Process a run using the ReAct agent loop with native tool calling."""
        from ..react_loop import run_react_loop
        from ..tools.registry import ToolContext

        system_prompt = self.context_builder.react_system_prompt(session_key, run.user_id, event)

        attachment_summary = self.context_builder.attachment_summary(event)
        attachments = self.context_builder.attachment_list(event)
        user_message: dict[str, Any] = {"role": "user", "content": event.text}
        if attachments:
            user_message["attachments"] = attachments
        messages: list[dict[str, Any]] = [user_message]
        if attachment_summary:
            messages.append({"role": "system", "content": attachment_summary})

        tool_context = self._build_tool_context(run)

        tools = self.tool_registry.all_tools() if self.tool_registry else []
        adapter = self._react_chat_adapter(run_id=run.run_id, session_key=session_key)
        current = self.state_manager.transition(run, current, RunState.EXECUTING, {"mode": "react_loop"})

        try:
            result = await run_react_loop(
                messages=messages,
                system_prompt=system_prompt,
                tools=tools,
                model_router=adapter,
                approval_manager=self.approval_manager,
                stream_sink=stream_sink,
                max_turns=self.AGENT_LOOP_MAX_TURNS * 2,
                session_key=session_key,
                tool_context=tool_context,
                run_id=run.run_id,
                approval_event=event.to_dict(),
                operator_layer=self.operator_layer,
            )
        except Exception as exc:
            self.state_manager.transition(run, current, RunState.FAILED, {"detail": str(exc)})
            raise

        for tc in result.tool_calls_made:
            self.store.append_run_event(
                run.run_id, session_key, "react.tool_call",
                {"tool": tc["tool"], "turn": tc.get("turn"), "error": tc.get("error")},
            )

        if result.pending_approval_id is not None:
            current = self.state_manager.transition(
                run,
                current,
                RunState.WAITING_APPROVAL,
                {"approval_id": result.pending_approval_id, "reply": result.reply},
            )
            self.memory.append_transcript(session_key, {"role": "assistant", "content": result.reply})
            self.store.append_run_event(
                run.run_id,
                session_key,
                "run.approval_requested",
                {"approval_id": result.pending_approval_id, "reply_text": result.reply},
            )
            return GatewayResult(
                run_id=run.run_id,
                session_key=session_key,
                status=current.value,
                reply_text=result.reply,
                approval_id=result.pending_approval_id,
            )

        reply_text = result.reply.strip()
        # Heartbeat with no model → default to OK
        if reply_text == "[No model available.]" and self.context_builder.is_heartbeat(event):
            reply_text = "HEARTBEAT_OK"
        suppress = reply_text == "NO_REPLY"

        current = self.state_manager.transition(run, current, RunState.RESPONDING, {"reply": reply_text})
        self.memory.append_transcript(session_key, {"role": "assistant", "content": reply_text})
        self.store.append_run_event(
            run.run_id, session_key, "run.response",
            {"reply_text": reply_text, "turns": result.turns, "tool_calls": len(result.tool_calls_made)},
        )
        current = self.state_manager.transition(run, current, RunState.COMPLETED, {"completed": True})

        self.memory.write_summary(
            session_key,
            "\n".join([
                "# Session Summary",
                "",
                f"- Last user message: {event.text}",
                f"- Run state: {current.value}",
                f"- Tool calls: {len(result.tool_calls_made)}",
                f"- Turns: {result.turns}",
                f"- Reply: {reply_text}",
            ]),
        )

        await self._drain_queue(session_key)
        return GatewayResult(
            run_id=run.run_id,
            session_key=session_key,
            status=current.value,
            reply_text=reply_text,
            suppress_delivery=suppress,
        )

    async def approve(self, approval_id: str) -> GatewayResult:
        """Handle approval — kept for ingest handler chat approval flow.

        Must not raise: returning an error result is better than wedging the session.
        """
        approval = self.store.get_approval(approval_id)
        if approval is None:
            return GatewayResult(
                run_id=approval_id,
                session_key="",
                status="error",
                reply_text="Approval not found.",
                queued=False,
            )
        if approval.status is not ApprovalStatus.PENDING:
            return GatewayResult(
                run_id=approval.run_id,
                session_key=approval.session_key,
                status="error",
                reply_text="That approval is not pending anymore.",
                queued=False,
            )

        run = self.store.get_run(approval.run_id)
        if run is None:
            return GatewayResult(
                run_id=approval.run_id,
                session_key=approval.session_key,
                status="error",
                reply_text="Associated run not found.",
                queued=False,
            )

        try:
            # Mark approved early so we don't re-prompt.
            self.store.update_approval_status(approval_id, ApprovalStatus.APPROVED)

            if approval.payload.get("kind") == "react_tool_call":
                return await self._approve_react_tool_call(approval)

            event = NormalizedInboundEvent.from_dict(approval.payload["event"])
            workflow_payload = approval.payload.get("workflow")
            if isinstance(workflow_payload, dict):
                workflow = WorkflowPlan.from_dict(workflow_payload)
            else:
                actions = [PlannedAction.from_dict(item) for item in approval.payload.get("actions", [])]
                workflow = self._workflow_from_actions("approved-actions", actions)

            pre_approval_results = list(approval.payload.get("pre_approval_results", []))
            current, execution_results = await self._execute_workflow(run, RunState.WAITING_APPROVAL, workflow)
            combined_results = list(pre_approval_results) + execution_results
            reply_text = await self.state_manager.finalize_reply(run, event, current, combined_results, workflow)
            final_current = self.state_manager.finalize_reply_text(run, event, current, combined_results, workflow, reply_text)
            await self._drain_queue(run.session_key)
            return GatewayResult(
                run_id=run.run_id,
                session_key=run.session_key,
                status=final_current.value,
                reply_text=reply_text,
                queued=False,
            )
        except Exception as exc:
            # Fail closed: do not wedge the session in WAITING_APPROVAL.
            self.store.append_run_event(
                run.run_id,
                run.session_key,
                "run.approval_error",
                {"approval_id": approval_id, "error": str(exc)},
            )
            try:
                self.state_manager.transition(
                    run,
                    RunState.WAITING_APPROVAL,
                    RunState.FAILED,
                    {"approval_id": approval_id, "error": str(exc)},
                )
            except Exception:
                self.store.set_run_state(run.run_id, RunState.FAILED)
            await self._drain_queue(run.session_key)
            return GatewayResult(
                run_id=run.run_id,
                session_key=run.session_key,
                status=RunState.FAILED.value,
                reply_text="Something went wrong while resuming after approval. Please retry.",
                queued=False,
            )

    async def _approve_react_tool_call(self, approval) -> GatewayResult:
        from ..react_loop import run_react_loop

        run = self.store.get_run(approval.run_id)
        if run is None:
            raise KeyError(f"unknown run id: {approval.run_id}")

        tool_name = str(approval.payload.get("tool_name", "")).strip()
        tool = None if self.tool_registry is None else self.tool_registry.get(tool_name)
        if tool is None:
            raise KeyError(f"unknown tool for approval {approval.approval_id}: {tool_name}")

        params = approval.payload.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"invalid params for approval {approval.approval_id}")
        tool_call_id = str(approval.payload.get("tool_call_id", "")).strip() or "approved-tool-call"
        messages = list(approval.payload.get("messages", []))
        event = NormalizedInboundEvent.from_dict(approval.payload["event"])
        tool_context = self._build_tool_context(run, approval_id=approval.approval_id)
        tools = self.tool_registry.all_tools() if self.tool_registry else []

        self.store.update_approval_status(approval.approval_id, ApprovalStatus.APPROVED)

        result = await tool.execute(params, ctx=tool_context)
        content = result.output
        if result.error:
            content = json.dumps({"error": result.error, "output": result.output})
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

        adapter = self._react_chat_adapter(run_id=run.run_id, session_key=run.session_key)
        prior_tool_calls = list(approval.payload.get("tool_calls_made", [])) + [
            {"tool": tool_name, "params": params, "turn": approval.payload.get("turn"), "result": result.output, "error": result.error}
        ]
        resumed = await run_react_loop(
            messages=messages,
            system_prompt=str(approval.payload.get("system_prompt", "")),
            tools=tools,
            model_router=adapter,
            approval_manager=self.approval_manager,
            max_turns=self.AGENT_LOOP_MAX_TURNS * 2,
            session_key=run.session_key,
            tool_context=tool_context,
            run_id=run.run_id,
            approval_event=event.to_dict(),
            initial_tool_calls_made=prior_tool_calls,
            operator_layer=self.operator_layer,
        )

        for tc in resumed.tool_calls_made[len(prior_tool_calls):]:
            self.store.append_run_event(
                run.run_id,
                run.session_key,
                "react.tool_call",
                {"tool": tc["tool"], "turn": tc.get("turn"), "error": tc.get("error")},
            )

        if resumed.pending_approval_id is not None:
            self.store.set_run_state(run.run_id, RunState.WAITING_APPROVAL)
            self.memory.append_transcript(run.session_key, {"role": "assistant", "content": resumed.reply})
            return GatewayResult(
                run_id=run.run_id,
                session_key=run.session_key,
                status=RunState.WAITING_APPROVAL.value,
                reply_text=resumed.reply,
                approval_id=resumed.pending_approval_id,
            )

        reply_text = resumed.reply.strip()
        current = self.state_manager.transition(run, RunState.WAITING_APPROVAL, RunState.RESPONDING, {"reply": reply_text})
        self.memory.append_transcript(run.session_key, {"role": "assistant", "content": reply_text})
        self.store.append_run_event(
            run.run_id,
            run.session_key,
            "run.response",
            {"reply_text": reply_text, "turns": resumed.turns, "tool_calls": len(resumed.tool_calls_made)},
        )
        current = self.state_manager.transition(run, current, RunState.COMPLETED, {"completed": True})
        await self._drain_queue(run.session_key)
        return GatewayResult(
            run_id=run.run_id,
            session_key=run.session_key,
            status=current.value,
            reply_text=reply_text,
        )

    def _workflow(
        self,
        intent: str,
        actions: list[PlannedAction],
        *,
        renderer: str = "default",
        skill_ids: list[str] | None = None,
    ) -> WorkflowPlan:
        return WorkflowPlan(
            workflow_id=uuid4().hex,
            intent=intent,
            steps=[WorkflowStep(step_id=f"step-{index}", action=action) for index, action in enumerate(actions, start=1)],
            renderer=renderer,
            approval_required=False,
            skill_ids=list(skill_ids or []),
        )

    def _workflow_from_actions(
        self,
        intent: str,
        actions: list[PlannedAction],
        *,
        renderer: str = "default",
        skill_ids: list[str] | None = None,
    ) -> WorkflowPlan:
        return self._workflow(intent, actions, renderer=renderer, skill_ids=skill_ids)

    def _fail_run(self, run: RunRecord, event: NormalizedInboundEvent, current: RunState, detail: str) -> GatewayResult:
        current = self.state_manager.transition(run, current, RunState.FAILED, {"detail": detail})
        reply_text = "The request was rejected by the capability policy."
        self.memory.append_transcript(run.session_key, {"role": "assistant", "content": reply_text})
        self.memory.write_summary(
            run.session_key,
            "\n".join([
                "# Session Summary",
                "",
                f"- Last user message: {event.text}",
                f"- Run state: {current.value}",
                f"- Failure detail: {detail}",
            ]),
        )
        return GatewayResult(run_id=run.run_id, session_key=run.session_key, status=current.value, reply_text=reply_text)

    async def _drain_queue(self, session_key: str) -> None:
        if self.store.get_active_run(session_key) is not None:
            return
        next_event = self.store.pop_next_queued_event(session_key)
        if next_event is not None:
            await self.ingest(next_event)

    def _is_deterministic_command(self, text: str) -> bool:
        """Check if text is a deterministic slash command (not react loop).

        NL patterns (memory, preferences, reminders, integrations) are handled
        by the LLM via tool calls in the react loop.
        """
        stripped = text.strip()
        lowered = stripped.lower()
        if not stripped.startswith("/"):
            return lowered == "usage"
        slash_prefixes = (
            "/memory", "/remember-", "/forget-", "/help", "/search ",
            "/shell ", "/fetch ", "/propose-patch ", "/gws ",
            "/quit", "/exit", "/q", "/mcp", "/events", "/usage",
            "/what-do-you-remember",
        )
        return any(lowered.startswith(p) or lowered == p.rstrip() for p in slash_prefixes)

    def _resolved_provider_name(self) -> str | None:
        profile = self.model_router.resolve_profile()
        return None if profile is None else f"{profile.provider}/{profile.model}"

    def _build_tool_context(
        self,
        run: RunRecord | None,
        *,
        approval_id: str | None = None,
        override_session_key: str | None = None,
        override_user_id: str | None = None,
        override_delivery_target=None,
        job_id: str | None = None,
        cancel_event=None,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
    ):
        from ..tools.registry import ToolContext

        session_key = override_session_key or (None if run is None else run.session_key) or ""
        user_id = override_user_id or (None if run is None else run.user_id) or ""
        delivery_target = override_delivery_target
        if delivery_target is None and run is not None:
            from ..models import DeliveryTarget

            delivery_target = DeliveryTarget(
                adapter=run.adapter,
                channel_id=run.channel_id,
                user_id=run.user_id,
            )
        command_runner = self.command_runner
        if command_runner is not None and any(item is not None for item in (cancel_event, stdout_path, stderr_path)):
            base_runner = command_runner

            class _JobCommandRunner:
                def __init__(self, runner, *, default_cancel_event, default_stdout_path, default_stderr_path):
                    self._runner = runner
                    self._default_cancel_event = default_cancel_event
                    self._default_stdout_path = default_stdout_path
                    self._default_stderr_path = default_stderr_path

                async def run(self, command, *, cwd=None, env=None, cancel_event=None, stdout_path=None, stderr_path=None):
                    kwargs = {
                        "cancel_event": self._default_cancel_event if cancel_event is None else cancel_event,
                        "stdout_path": self._default_stdout_path if stdout_path is None else stdout_path,
                        "stderr_path": self._default_stderr_path if stderr_path is None else stderr_path,
                    }
                    kwargs = {key: value for key, value in kwargs.items() if value is not None}
                    return await self._runner.run(command, cwd=cwd, env=env, **kwargs)

                async def run_argv(self, argv, *, cwd=None, env=None, cancel_event=None, stdout_path=None, stderr_path=None):
                    kwargs = {
                        "cancel_event": self._default_cancel_event if cancel_event is None else cancel_event,
                        "stdout_path": self._default_stdout_path if stdout_path is None else stdout_path,
                        "stderr_path": self._default_stderr_path if stderr_path is None else stderr_path,
                    }
                    kwargs = {key: value for key, value in kwargs.items() if value is not None}
                    return await self._runner.run_argv(argv, cwd=cwd, env=env, **kwargs)

            command_runner = _JobCommandRunner(
                base_runner,
                default_cancel_event=cancel_event,
                default_stdout_path=stdout_path,
                default_stderr_path=stderr_path,
            )

        return ToolContext(
            session_key=session_key,
            user_id=user_id,
            memory=self.memory,
            store=self.store,
            config=getattr(self, "_config", None),
            run_id=None if run is None else run.run_id,
            delivery_target=delivery_target,
            job_service=getattr(self, "_job_service", None),
            job_id=job_id,
            approval_id=approval_id,
            cancel_event=cancel_event,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            skill_registry=self.skills,
            command_runner=command_runner,
            diagnosis_engine=self.diagnosis_engine,
        )

    def _react_chat_adapter(self, *, run_id: str | None = None, session_key: str | None = None):
        class _ChatAdapter:
            def __init__(self, router: ModelRouter, *, current_run_id: str | None, current_session_key: str | None):
                self._router = router
                self._run_id = current_run_id
                self._session_key = current_session_key

            async def chat(self, msgs, *, system=None, tools=None, stream_sink=None):
                return await self._router.chat(
                    msgs,
                    system_prompt=system,
                    tools=tools,
                    sink=stream_sink,
                    run_id=self._run_id,
                    session_key=self._session_key,
                )

        return _ChatAdapter(self.model_router, current_run_id=run_id, current_session_key=session_key)

    def _resolve_step_action(self, action: PlannedAction, execution_results: list[dict[str, Any]]) -> PlannedAction:
        """Resolve $last. references in action payloads from prior step results."""
        if not execution_results:
            return action
        payload = json.loads(json.dumps(action.payload, ensure_ascii=True))
        last_artifacts = execution_results[-1].get("artifacts", {})
        last_output = last_artifacts.get("output", {})

        def resolve(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("$last."):
                key = value.removeprefix("$last.")
                if isinstance(last_output, dict) and key in last_output:
                    return last_output[key]
                return last_artifacts.get(key, value)
            if isinstance(value, list):
                return [resolve(item) for item in value]
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            return value

        return PlannedAction(action=action.action, payload=resolve(payload))

    def _parse_model_json(self, text: str | None) -> dict[str, Any] | None:
        if not text:
            return None
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:].strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if not match:
                return None
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return parsed if isinstance(parsed, dict) else None
