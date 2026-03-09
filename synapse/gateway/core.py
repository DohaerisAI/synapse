from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..broker import CapabilityBroker
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
from ..session import SessionStateMachine
from ..streaming.sink import StreamSink
from ..skills import SkillRegistry
from ..store import SQLiteStore
from ..workspace import WorkspaceStore

from .agent_loop import AgentLoop
from .approval import ApprovalHandler
from .context import ContextBuilder
from .executor import WorkflowExecutor
from .extractors import RequestExtractors
from .action_planner import ActionPlanner
from .gws_planner import GWSPlanner
from .ingest import IngestHandler
from .planner import WorkflowPlanner
from .renderer import ReplyRenderer
from .state import StateManager


class Gateway:
    AGENT_LOOP_MAX_TURNS = 4

    # Expose PlannedAction for sub-modules that need it via gw reference
    _PlannedAction = PlannedAction

    def __init__(
        self,
        *,
        store: SQLiteStore,
        memory: MemoryStore,
        workspace: WorkspaceStore,
        skills: SkillRegistry,
        broker: CapabilityBroker,
        state_machine: SessionStateMachine,
        host_executor: HostExecutor,
        isolated_executor: IsolatedExecutor,
        model_router: ModelRouter,
        agent_name: str = "Agent",
        assistant_instructions: str = "",
        gws_planner_instructions: str = "",
        heartbeat_path: Path | None = None,
        hooks: HookRunner | None = None,
    ) -> None:
        self.store = store
        self.memory = memory
        self.workspace = workspace
        self.skills = skills
        self.broker = broker
        self.state_machine = state_machine
        self.host_executor = host_executor
        self.isolated_executor = isolated_executor
        self.model_router = model_router
        self.agent_name = agent_name.strip() or "Agent"
        self.assistant_instructions = assistant_instructions.strip()
        self.gws_planner_instructions = gws_planner_instructions.strip()
        self.heartbeat_path = heartbeat_path
        self.hooks = hooks or HookRunner()

        # Sub-handlers
        self.approval_handler = ApprovalHandler(self)
        self.context_builder = ContextBuilder(self)
        self.extractors = RequestExtractors(self)
        self.gws_planner = GWSPlanner(self)
        self.action_planner = ActionPlanner(self)
        self.executor = WorkflowExecutor(self)
        self.state_manager = StateManager(self)
        self.renderer = ReplyRenderer(self)
        self.planner = WorkflowPlanner(self)
        self.agent_loop = AgentLoop(self)
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

        pending_input = await self.planner.plan_pending_input(event, session_key=session_key)
        if pending_input is not None:
            current = self.state_manager.transition(
                run,
                current,
                RunState.PLANNED,
                {"pending_input_kind": pending_input["kind"]},
            )
            input_request = self.store.create_input_request(
                run.run_id,
                session_key,
                kind=str(pending_input["kind"]),
                payload=dict(pending_input["payload"]),
                prompt=str(pending_input["prompt"]),
            )
            self.state_manager.transition(run, current, RunState.WAITING_INPUT, {"input_id": input_request.input_id})
            reply_text = input_request.prompt
            self.memory.append_transcript(session_key, {"role": "assistant", "content": reply_text})
            self.store.append_run_event(
                run.run_id,
                session_key,
                "workflow.paused_for_input",
                {"input_id": input_request.input_id, "kind": input_request.kind, "payload": input_request.payload},
            )
            return GatewayResult(
                run_id=run.run_id,
                session_key=session_key,
                status=RunState.WAITING_INPUT.value,
                reply_text=reply_text,
            )

        workflow = await self.planner.plan_workflow(event, session_key=session_key)
        self.store.append_run_event(
            run.run_id,
            session_key,
            "workflow.planned",
            workflow.to_dict(),
        )
        current = self.state_manager.transition(
            run,
            current,
            RunState.PLANNED,
            {"workflow_id": workflow.workflow_id, "step_count": len(workflow.steps), "intent": workflow.intent},
        )

        if workflow.intent == "chat.respond" and not workflow.steps and not self.context_builder.is_heartbeat(event):
            result = await self.agent_loop.run(run, event, current, stream_sink=stream_sink)
            if result.status == RunState.COMPLETED.value:
                await self._drain_queue(session_key)
            return result

        decisions = [(step, self.broker.decide(step.action)) for step in workflow.steps]
        disallowed = [pair for pair in decisions if not pair[1].allowed]
        if disallowed:
            return self._fail_run(run, event, current, "one or more planned actions were rejected by policy")

        workflow = self.approval_handler.apply_decisions_to_workflow(workflow, decisions)
        if workflow.approval_required:
            pre_approval_workflow, approval_workflow = self.approval_handler.split_workflow_for_approval(workflow)
            execution_results: list[dict[str, Any]] = []
            if pre_approval_workflow.steps:
                current, execution_results = await self.executor.execute_workflow(run, current, pre_approval_workflow)
                if any(not item["success"] for item in execution_results):
                    reply_text = await self.state_manager.finalize_reply(run, event, current, execution_results, workflow)
                    final_current = self.state_manager.finalize_reply_text(run, event, current, execution_results, workflow, reply_text)
                    await self._drain_queue(session_key)
                    return GatewayResult(
                        run_id=run.run_id,
                        session_key=session_key,
                        status=final_current.value,
                        reply_text=reply_text,
                    )
            approval = self.store.create_approval(
                run.run_id,
                session_key,
                "workflow.execute",
                {
                    "event": event.to_dict(),
                    "workflow": approval_workflow.to_dict(),
                    "pre_approval_results": execution_results,
                },
            )
            self.state_manager.transition(run, current, RunState.WAITING_APPROVAL, {"approval_id": approval.approval_id})
            reply_text = self.approval_handler.approval_reply_text(approval_workflow)
            self.memory.append_transcript(session_key, {"role": "assistant", "content": reply_text})
            self.store.append_run_event(
                run.run_id,
                session_key,
                "workflow.paused_for_approval",
                {"approval_id": approval.approval_id, "workflow": approval_workflow.to_dict(), "pre_approval_results": execution_results},
            )
            return GatewayResult(
                run_id=run.run_id,
                session_key=session_key,
                status=RunState.WAITING_APPROVAL.value,
                reply_text=reply_text,
                approval_id=approval.approval_id,
            )

        current, execution_results = await self.executor.execute_workflow(run, current, workflow)
        reply_text = await self.state_manager.finalize_reply(run, event, current, execution_results, workflow)
        final_current = self.state_manager.finalize_reply_text(run, event, current, execution_results, workflow, reply_text)
        await self._drain_queue(session_key)
        return GatewayResult(
            run_id=run.run_id,
            session_key=session_key,
            status=final_current.value,
            reply_text=reply_text,
        )

    async def approve(self, approval_id: str) -> GatewayResult:
        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"unknown approval id: {approval_id}")
        if approval.status is not ApprovalStatus.PENDING:
            raise ValueError(f"approval {approval_id} is not pending")

        run = self.store.get_run(approval.run_id)
        if run is None:
            raise KeyError(f"unknown run id: {approval.run_id}")

        event = NormalizedInboundEvent.from_dict(approval.payload["event"])
        workflow_payload = approval.payload.get("workflow")
        if isinstance(workflow_payload, dict):
            workflow = WorkflowPlan.from_dict(workflow_payload)
        else:
            actions = [PlannedAction.from_dict(item) for item in approval.payload.get("actions", [])]
            workflow = self._workflow_from_actions("approved-actions", actions)
        pre_approval_results = list(approval.payload.get("pre_approval_results", []))
        self.store.update_approval_status(approval_id, ApprovalStatus.APPROVED)
        if bool(approval.payload.get("agent_loop")):
            current, execution_results = await self.executor.execute_workflow(run, RunState.WAITING_APPROVAL, workflow)
            combined_results = list(pre_approval_results) + execution_results
            reply_text = await self.state_manager.finalize_reply(run, event, current, combined_results, workflow)
            final_current = self.state_manager.finalize_reply_text(run, event, current, combined_results, workflow, reply_text)
            return GatewayResult(
                run_id=run.run_id,
                session_key=run.session_key,
                status=final_current.value,
                reply_text=reply_text,
            )
        result = await self._execute_and_respond(run, event, RunState.WAITING_APPROVAL, workflow, pre_approval_results)
        await self._drain_queue(run.session_key)
        return result

    async def _execute_and_respond(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        current: RunState,
        workflow: WorkflowPlan,
        pre_approval_results: list[dict[str, Any]] | None = None,
    ) -> GatewayResult:
        current, execution_results = await self.executor.execute_workflow(run, current, workflow)
        combined_results = list(pre_approval_results or []) + execution_results
        reply_text = await self.state_manager.finalize_reply(run, event, current, combined_results, workflow)
        final_current = self.state_manager.finalize_reply_text(run, event, current, combined_results, workflow, reply_text)
        return GatewayResult(
            run_id=run.run_id,
            session_key=run.session_key,
            status=final_current.value,
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
            "\n".join(
                [
                    "# Session Summary",
                    "",
                    f"- Last user message: {event.text}",
                    f"- Run state: {current.value}",
                    f"- Failure detail: {detail}",
                ]
            ),
        )
        return GatewayResult(run_id=run.run_id, session_key=run.session_key, status=current.value, reply_text=reply_text)

    async def _drain_queue(self, session_key: str) -> None:
        if self.store.get_active_run(session_key) is not None:
            return
        next_event = self.store.pop_next_queued_event(session_key)
        if next_event is not None:
            await self.ingest(next_event)

    def _resolved_provider_name(self) -> str | None:
        profile = self.model_router.resolve_profile()
        return None if profile is None else f"{profile.provider}/{profile.model}"

    # Backward-compat delegations for tests that access internal methods
    async def _intent_mode(self, event: NormalizedInboundEvent, *, session_key: str | None = None) -> str:
        return await self.planner._intent_mode(event, session_key=session_key)

    async def _run_skill_gws_planner(self, text: str, **kwargs: Any) -> dict[str, Any] | None:
        return await self.gws_planner.run_skill_gws_planner(text, **kwargs)

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
