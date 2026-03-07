from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..models import (
    ApprovalStatus,
    GatewayResult,
    InputStatus,
    NormalizedInboundEvent,
    PlannedAction,
    RunRecord,
    RunState,
    WorkflowPlan,
)

if TYPE_CHECKING:
    from .core import Gateway


class IngestHandler:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    async def resolve_pending_input(self, active_run: RunRecord, event: NormalizedInboundEvent) -> GatewayResult | None:
        gw = self._gw
        if active_run.state is not RunState.WAITING_INPUT:
            return None
        pending = gw.store.get_pending_input_for_session(active_run.session_key)
        if pending is None:
            return None
        lowered = event.text.strip().lower()
        if gw.approval_handler.is_approval_rejection(lowered):
            gw.memory.append_transcript(active_run.session_key, {"role": "user", "content": event.text, "message_id": event.message_id, "kind": "input"})
            gw.store.update_input_request(pending.input_id, status=InputStatus.CANCELLED)
            gw.state_manager.transition(active_run, RunState.WAITING_INPUT, RunState.CANCELLED, {"input_id": pending.input_id, "text": event.text})
            reply_text = "Okay, I cancelled that request."
            gw.memory.append_transcript(active_run.session_key, {"role": "assistant", "content": reply_text})
            gw.store.append_run_event(active_run.run_id, active_run.session_key, "run.chat_input.cancelled", {"input_id": pending.input_id, "text": event.text})
            await gw._drain_queue(active_run.session_key)
            return GatewayResult(run_id=active_run.run_id, session_key=active_run.session_key, status=RunState.CANCELLED.value, reply_text=reply_text)
        return await self.continue_pending_input(active_run, pending, event)

    async def continue_pending_input(
        self,
        run: RunRecord,
        pending: Any,
        event: NormalizedInboundEvent,
    ) -> GatewayResult:
        gw = self._gw
        gw.memory.append_transcript(run.session_key, {"role": "user", "content": event.text, "message_id": event.message_id, "kind": "input"})
        updated_payload, prompt, workflow = await gw.state_manager.merge_pending_input_payload(pending, event, run)
        if workflow is None:
            gw.store.update_input_request(pending.input_id, payload=updated_payload, prompt=prompt)
            gw.store.append_run_event(
                run.run_id,
                run.session_key,
                "workflow.input.updated",
                {"input_id": pending.input_id, "kind": pending.kind, "payload": updated_payload},
            )
            gw.memory.append_transcript(run.session_key, {"role": "assistant", "content": prompt})
            return GatewayResult(
                run_id=run.run_id,
                session_key=run.session_key,
                status=RunState.WAITING_INPUT.value,
                reply_text=prompt,
            )

        gw.store.update_input_request(pending.input_id, status=InputStatus.RESOLVED, payload=updated_payload)
        gw.store.append_run_event(
            run.run_id,
            run.session_key,
            "workflow.input.resolved",
            {"input_id": pending.input_id, "kind": pending.kind, "payload": updated_payload, "workflow": workflow.to_dict()},
        )
        current = gw.state_manager.transition(
            run,
            RunState.WAITING_INPUT,
            RunState.PLANNED,
            {"workflow_id": workflow.workflow_id, "step_count": len(workflow.steps), "intent": workflow.intent},
        )
        if pending.kind == "agent.loop" and workflow.intent == "chat.respond" and not workflow.steps:
            result = await gw.agent_loop.run(
                run,
                event,
                current,
                prior_results=list(updated_payload.get("prior_results", [])),
                continuation_text=str(updated_payload.get("continuation_text", event.text)).strip() or event.text,
            )
            await gw._drain_queue(run.session_key)
            return result
        decisions = [(step, gw.broker.decide(step.action)) for step in workflow.steps]
        workflow = gw.approval_handler.apply_decisions_to_workflow(workflow, decisions)
        if workflow.approval_required:
            pre_approval_workflow, approval_workflow = gw.approval_handler.split_workflow_for_approval(workflow)
            execution_results: list[dict[str, Any]] = []
            if pre_approval_workflow.steps:
                current, execution_results = await gw.executor.execute_workflow(run, current, pre_approval_workflow)
                if any(not item["success"] for item in execution_results):
                    reply_text = await gw.state_manager.finalize_reply(run, event, current, execution_results, workflow)
                    final_current = gw.state_manager.finalize_reply_text(run, event, current, execution_results, workflow, reply_text)
                    await gw._drain_queue(run.session_key)
                    return GatewayResult(
                        run_id=run.run_id,
                        session_key=run.session_key,
                        status=final_current.value,
                        reply_text=reply_text,
                    )
            approval = gw.store.create_approval(
                run.run_id,
                run.session_key,
                "workflow.execute",
                {
                    "event": event.to_dict(),
                    "workflow": approval_workflow.to_dict(),
                    "pre_approval_results": execution_results,
                },
            )
            gw.state_manager.transition(run, current, RunState.WAITING_APPROVAL, {"approval_id": approval.approval_id})
            reply_text = gw.approval_handler.approval_reply_text(approval_workflow)
            gw.memory.append_transcript(run.session_key, {"role": "assistant", "content": reply_text})
            gw.store.append_run_event(
                run.run_id,
                run.session_key,
                "workflow.paused_for_approval",
                {"approval_id": approval.approval_id, "workflow": approval_workflow.to_dict(), "pre_approval_results": execution_results},
            )
            return GatewayResult(
                run_id=run.run_id,
                session_key=run.session_key,
                status=RunState.WAITING_APPROVAL.value,
                reply_text=reply_text,
                approval_id=approval.approval_id,
            )

        current, execution_results = await gw.executor.execute_workflow(run, current, workflow)
        reply_text = await gw.state_manager.finalize_reply(run, event, current, execution_results, workflow)
        final_current = gw.state_manager.finalize_reply_text(run, event, current, execution_results, workflow, reply_text)
        await gw._drain_queue(run.session_key)
        return GatewayResult(
            run_id=run.run_id,
            session_key=run.session_key,
            status=final_current.value,
            reply_text=reply_text,
        )

    async def resolve_chat_approval(self, active_run: RunRecord, event: NormalizedInboundEvent) -> GatewayResult | None:
        gw = self._gw
        if active_run.state is not RunState.WAITING_APPROVAL:
            return None
        session_key = active_run.session_key
        pending = gw.store.get_pending_approval_for_session(session_key)
        if pending is None:
            return None
        lowered = event.text.strip().lower()
        if gw.approval_handler.is_approval_confirmation(lowered):
            gw.memory.append_transcript(session_key, {"role": "user", "content": event.text, "message_id": event.message_id, "kind": "approval"})
            gw.store.append_run_event(active_run.run_id, session_key, "run.chat_approval.approved", {"approval_id": pending.approval_id, "text": event.text})
            return await gw.approve(pending.approval_id)
        if gw.approval_handler.is_approval_rejection(lowered):
            gw.memory.append_transcript(session_key, {"role": "user", "content": event.text, "message_id": event.message_id, "kind": "approval"})
            gw.store.update_approval_status(pending.approval_id, ApprovalStatus.REJECTED)
            gw.state_manager.transition(active_run, RunState.WAITING_APPROVAL, RunState.CANCELLED, {"approval_id": pending.approval_id, "text": event.text})
            reply_text = "Okay, I cancelled that request."
            gw.memory.append_transcript(session_key, {"role": "assistant", "content": reply_text})
            gw.store.append_run_event(active_run.run_id, session_key, "run.chat_approval.rejected", {"approval_id": pending.approval_id, "text": event.text})
            gw.memory.write_summary(
                session_key,
                "\n".join(
                    [
                        "# Session Summary",
                        "",
                        f"- Last user message: {event.text}",
                        "- Run state: CANCELLED",
                        "- Detail: user rejected the pending approval in chat",
                    ]
                ),
            )
            await gw._drain_queue(session_key)
            return GatewayResult(run_id=active_run.run_id, session_key=session_key, status=RunState.CANCELLED.value, reply_text=reply_text)
        return None
