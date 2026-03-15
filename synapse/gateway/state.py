from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..models import (
    NormalizedInboundEvent,
    PendingInputRecord,
    RunRecord,
    RunState,
    WorkflowPlan,
    utc_now,
)

if TYPE_CHECKING:
    from .core import Gateway


class StateManager:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    def transition(self, run: RunRecord, current: RunState, target: RunState, payload: dict[str, Any]) -> RunState:
        gw = self._gw
        gw.state_machine.assert_transition(current, target)
        gw.store.set_run_state(run.run_id, target)
        gw.store.append_run_event(run.run_id, run.session_key, f"state.{target.value.lower()}", payload)
        return target

    async def finalize_reply(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        current: RunState,
        execution_results: list[dict[str, Any]],
        workflow: WorkflowPlan,
    ) -> str:
        """Build a simple reply from execution results (inline, no LLM renderer)."""
        if not execution_results:
            return "Done."
        parts: list[str] = []
        for result in execution_results:
            action = result.get("action", "unknown")
            detail = result.get("detail", "")
            if result.get("success"):
                parts.append(f"{action}: {detail}" if detail else action)
            else:
                parts.append(f"{action} failed: {detail}" if detail else f"{action} failed")
        return "\n".join(parts)

    def finalize_reply_text(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        current: RunState,
        execution_results: list[dict[str, Any]],
        workflow: WorkflowPlan,
        reply_text: str,
    ) -> RunState:
        gw = self._gw
        current = self.transition(run, current, RunState.RESPONDING, {"reply": reply_text})
        gw.memory.append_transcript(run.session_key, {"role": "assistant", "content": reply_text})
        gw.store.append_run_event(
            run.run_id,
            run.session_key,
            "run.response",
            {"reply_text": reply_text, "results": execution_results},
        )
        current = self.transition(run, current, RunState.COMPLETED, {"completed": True})
        gw.memory.write_summary(
            run.session_key,
            "\n".join([
                "# Session Summary",
                "",
                f"- Last user message: {event.text}",
                f"- Run state: {current.value}",
                f"- Actions: {', '.join(action['action'] for action in execution_results) if execution_results else 'none'}",
                f"- Reply: {reply_text}",
            ]),
        )
        self._update_current_task(run, event, workflow, reply_text, execution_results)
        if workflow.skill_ids and any(item["success"] for item in execution_results if item["action"].startswith("gws.")):
            commands = [
                str(item.get("artifacts", {}).get("command", "")).strip()
                for item in execution_results
                if item["action"].startswith("gws.")
            ]
            commands = [item for item in commands if item]
            note = next((str(item["detail"]).strip() for item in execution_results if item["action"].startswith("gws.") and item["success"]), "")
            gw.memory.append_skill_operation(
                skill_ids=workflow.skill_ids,
                intent=workflow.intent,
                commands=commands,
                note=note,
            )
            gw.workspace.promote_playbook(
                intent=workflow.intent,
                skill_ids=workflow.skill_ids,
                commands=commands,
                note=note,
            )
        return current

    def _update_current_task(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        workflow: WorkflowPlan,
        reply_text: str,
        execution_results: list[dict[str, Any]],
    ) -> None:
        gw = self._gw
        if gw.context_builder.is_heartbeat(event):
            return
        existing_task = gw.memory.read_current_task(run.session_key) or {}
        transcript_entries = gw.memory.read_recent_transcript(run.session_key, limit=6)
        transcript_excerpt = []
        for entry in transcript_entries:
            role = str(entry.get("role", "unknown")).strip()
            content = str(entry.get("content", "")).strip()
            if role and content:
                transcript_excerpt.append({"role": role, "content": content})
        actions = [
            item["action"]
            for item in execution_results
            if item.get("action") and not str(item.get("action", "")).startswith("agent.loop.")
        ]
        if not actions:
            actions = [str(item) for item in existing_task.get("actions", []) if str(item).strip()]
        hinted_skill_ids = list(workflow.skill_ids)
        if not hinted_skill_ids:
            hinted_skill_ids = gw.skills.select_candidates(event.text, limit=4)
        if not hinted_skill_ids:
            hinted_skill_ids = [str(item) for item in existing_task.get("skill_ids", []) if str(item).strip()]
        intent = workflow.intent
        if intent == "chat.respond":
            prior_intent = str(existing_task.get("intent", "")).strip()
            if prior_intent and prior_intent != "chat.respond":
                intent = prior_intent
        title = event.text.strip()[:160]
        if len(title) < 24:
            prior_title = str(existing_task.get("title", "")).strip()
            if prior_title:
                title = prior_title
        task = {
            "title": title,
            "intent": intent,
            "mode": "act" if workflow.steps else str(existing_task.get("mode", "chat")).strip() or "chat",
            "latest_user_request": event.text.strip(),
            "latest_reply": reply_text.strip(),
            "skill_ids": hinted_skill_ids,
            "actions": actions,
            "updated_at": utc_now().isoformat(),
            "transcript_excerpt": transcript_excerpt,
        }
        gw.memory.write_current_task(run.session_key, task)
