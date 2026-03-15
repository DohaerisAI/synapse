from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..models import (
    ApprovalStatus,
    GatewayResult,
    InputStatus,
    NormalizedInboundEvent,
    RunRecord,
    RunState,
)

if TYPE_CHECKING:
    from .core import Gateway

# Approval confirmation/rejection phrases (previously in ApprovalHandler)
_CONFIRM_PHRASES = {"yes", "y", "approve", "approved", "go ahead", "do it", "yes go ahead", "sure", "okay", "ok"}
_REJECT_PHRASES = {"no", "n", "cancel", "reject", "stop", "don't", "do not"}


def _is_approval_confirmation(lowered: str) -> bool:
    return lowered in _CONFIRM_PHRASES or lowered.startswith(("yes ", "approve ", "go ahead "))


def _is_approval_rejection(lowered: str) -> bool:
    return lowered in _REJECT_PHRASES or lowered.startswith(("no ", "cancel ", "reject ", "stop "))


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
        if _is_approval_rejection(lowered):
            gw.memory.append_transcript(active_run.session_key, {"role": "user", "content": event.text, "message_id": event.message_id, "kind": "input"})
            gw.store.update_input_request(pending.input_id, status=InputStatus.CANCELLED)
            gw.state_manager.transition(active_run, RunState.WAITING_INPUT, RunState.CANCELLED, {"input_id": pending.input_id, "text": event.text})
            reply_text = "Okay, I cancelled that request."
            gw.memory.append_transcript(active_run.session_key, {"role": "assistant", "content": reply_text})
            gw.store.append_run_event(active_run.run_id, active_run.session_key, "run.chat_input.cancelled", {"input_id": pending.input_id, "text": event.text})
            await gw._drain_queue(active_run.session_key)
            return GatewayResult(run_id=active_run.run_id, session_key=active_run.session_key, status=RunState.CANCELLED.value, reply_text=reply_text)
        # For other input, cancel the pending request and let the new message start fresh
        gw.store.update_input_request(pending.input_id, status=InputStatus.CANCELLED)
        gw.state_manager.transition(active_run, RunState.WAITING_INPUT, RunState.CANCELLED, {"input_id": pending.input_id, "text": event.text, "reason": "superseded"})
        await gw._drain_queue(active_run.session_key)
        return None

    async def resolve_chat_approval(self, active_run: RunRecord, event: NormalizedInboundEvent) -> GatewayResult | None:
        gw = self._gw
        if active_run.state is not RunState.WAITING_APPROVAL:
            return None
        session_key = active_run.session_key
        pending = gw.store.get_pending_approval_for_session(session_key)
        if pending is None:
            return None
        lowered = event.text.strip().lower()
        if _is_approval_confirmation(lowered):
            gw.memory.append_transcript(session_key, {"role": "user", "content": event.text, "message_id": event.message_id, "kind": "approval"})
            gw.store.append_run_event(active_run.run_id, session_key, "run.chat_approval.approved", {"approval_id": pending.approval_id, "text": event.text})
            return await gw.approve(pending.approval_id)
        if _is_approval_rejection(lowered):
            gw.memory.append_transcript(session_key, {"role": "user", "content": event.text, "message_id": event.message_id, "kind": "approval"})
            gw.store.update_approval_status(pending.approval_id, ApprovalStatus.REJECTED)
            gw.state_manager.transition(active_run, RunState.WAITING_APPROVAL, RunState.CANCELLED, {"approval_id": pending.approval_id, "text": event.text})
            reply_text = "Okay, I cancelled that request."
            gw.memory.append_transcript(session_key, {"role": "assistant", "content": reply_text})
            gw.store.append_run_event(active_run.run_id, session_key, "run.chat_approval.rejected", {"approval_id": pending.approval_id, "text": event.text})
            gw.memory.write_summary(
                session_key,
                "\n".join([
                    "# Session Summary",
                    "",
                    f"- Last user message: {event.text}",
                    "- Run state: CANCELLED",
                    "- Detail: user rejected the pending approval in chat",
                ]),
            )
            await gw._drain_queue(session_key)
            return GatewayResult(run_id=active_run.run_id, session_key=session_key, status=RunState.CANCELLED.value, reply_text=reply_text)
        return None
