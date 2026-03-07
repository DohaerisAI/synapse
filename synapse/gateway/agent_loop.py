from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ..capabilities import DEFAULT_CAPABILITY_REGISTRY
from ..models import (
    GatewayResult,
    NormalizedInboundEvent,
    PlannedAction,
    RunRecord,
    RunState,
    WorkflowPlan,
)

if TYPE_CHECKING:
    from .core import Gateway


class AgentLoop:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    async def run(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        current: RunState,
        *,
        prior_results: list[dict[str, Any]] | None = None,
        continuation_text: str | None = None,
    ) -> GatewayResult:
        gw = self._gw
        execution_results = list(prior_results or [])
        loop_text = continuation_text or event.text
        current_task = gw.memory.read_current_task(run.session_key) or {}
        last_workflow = gw._workflow("chat.respond", [], renderer="default")
        for turn in range(1, gw.AGENT_LOOP_MAX_TURNS + 1):
            gw.store.append_run_event(
                run.run_id,
                run.session_key,
                "model.turn.started",
                {"turn": turn, "result_count": len(execution_results)},
            )
            directive = await self._run_turn(run, event, loop_text, execution_results)
            gw.store.append_run_event(
                run.run_id,
                run.session_key,
                "model.turn.completed",
                {"turn": turn, "directive": directive},
            )
            if directive is None:
                break
            status = str(directive.get("status", "reply")).strip().lower()
            if status == "reply":
                reply_text = str(directive.get("reply", "")).strip() or "Okay."
                skill_ids = [str(item) for item in directive.get("skill_ids", []) if str(item).strip()]
                if self._requires_structured_action(loop_text, current_task, execution_results):
                    execution_results.append(
                        {
                            "action": "agent.loop.validation",
                            "success": False,
                            "detail": "This turn is continuing an operational task. Emit tool_calls or ask_input instead of a plain final reply.",
                            "artifacts": {},
                        }
                    )
                    continue
                last_workflow = gw._workflow("chat.respond", [], renderer="default", skill_ids=skill_ids)
                final_current = gw.state_manager.finalize_reply_text(run, event, current, execution_results, last_workflow, reply_text)
                return GatewayResult(
                    run_id=run.run_id,
                    session_key=run.session_key,
                    status=final_current.value,
                    reply_text=reply_text,
                )
            if status == "ask_input":
                prompt = str(directive.get("prompt", "")).strip() or "I need a bit more information to continue."
                payload = {
                    "base_event": event.to_dict(),
                    "prior_results": execution_results,
                    "draft": dict(directive.get("draft", {})) if isinstance(directive.get("draft"), dict) else {},
                }
                input_request = gw.store.create_input_request(
                    run.run_id,
                    run.session_key,
                    kind="agent.loop",
                    payload=payload,
                    prompt=prompt,
                )
                gw.state_manager.transition(run, current, RunState.WAITING_INPUT, {"input_id": input_request.input_id})
                gw.memory.append_transcript(run.session_key, {"role": "assistant", "content": prompt})
                return GatewayResult(
                    run_id=run.run_id,
                    session_key=run.session_key,
                    status=RunState.WAITING_INPUT.value,
                    reply_text=prompt,
                )
            if status != "tool_calls":
                break
            tool_calls = directive.get("tool_calls", [])
            if not isinstance(tool_calls, list) or not tool_calls:
                break
            skill_ids = [str(item) for item in directive.get("skill_ids", []) if str(item).strip()]
            actions = [PlannedAction.from_dict(item) for item in tool_calls if isinstance(item, dict)]
            if not actions:
                break
            last_workflow = gw._workflow_from_actions(
                str(directive.get("intent", "agent.loop")),
                actions,
                renderer=str(directive.get("renderer", "default")),
                skill_ids=skill_ids,
            )
            decisions = [(step, gw.broker.decide(step.action)) for step in last_workflow.steps]
            disallowed = [pair for pair in decisions if not pair[1].allowed]
            if disallowed:
                return gw._fail_run(run, event, current, "one or more model-planned actions were rejected by policy")
            last_workflow = gw.approval_handler.apply_decisions_to_workflow(last_workflow, decisions)
            if last_workflow.approval_required:
                pre_approval_workflow, approval_workflow = gw.approval_handler.split_workflow_for_approval(last_workflow)
                if pre_approval_workflow.steps:
                    current, pre_results = await gw.executor.execute_workflow(run, current, pre_approval_workflow)
                    execution_results.extend(pre_results)
                    if any(not item["success"] for item in pre_results):
                        reply_text = await gw.state_manager.finalize_reply(run, event, current, execution_results, last_workflow)
                        final_current = gw.state_manager.finalize_reply_text(run, event, current, execution_results, last_workflow, reply_text)
                        return GatewayResult(
                            run_id=run.run_id,
                            session_key=run.session_key,
                            status=final_current.value,
                            reply_text=reply_text,
                        )
                approval = gw.store.create_approval(
                    run.run_id,
                    run.session_key,
                    "agent.loop.execute",
                    {
                        "event": event.to_dict(),
                        "workflow": approval_workflow.to_dict(),
                        "pre_approval_results": execution_results,
                        "agent_loop": True,
                    },
                )
                gw.state_manager.transition(run, current, RunState.WAITING_APPROVAL, {"approval_id": approval.approval_id})
                reply_text = gw.approval_handler.approval_reply_text(approval_workflow)
                gw.memory.append_transcript(run.session_key, {"role": "assistant", "content": reply_text})
                return GatewayResult(
                    run_id=run.run_id,
                    session_key=run.session_key,
                    status=RunState.WAITING_APPROVAL.value,
                    reply_text=reply_text,
                    approval_id=approval.approval_id,
                )
            current, step_results = await gw.executor.execute_workflow(run, current, last_workflow)
            execution_results.extend(step_results)
            if any(not item["success"] for item in step_results):
                reply_text = await gw.state_manager.finalize_reply(run, event, current, execution_results, last_workflow)
                final_current = gw.state_manager.finalize_reply_text(run, event, current, execution_results, last_workflow, reply_text)
                return GatewayResult(
                    run_id=run.run_id,
                    session_key=run.session_key,
                    status=final_current.value,
                    reply_text=reply_text,
                )
        reply_text = "I couldn't finish that request cleanly."
        final_current = gw.state_manager.finalize_reply_text(run, event, current, execution_results, last_workflow, reply_text)
        return GatewayResult(
            run_id=run.run_id,
            session_key=run.session_key,
            status=final_current.value,
            reply_text=reply_text,
        )

    async def _run_turn(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        text: str,
        execution_results: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        gw = self._gw
        system_prompt = self._agent_loop_system_prompt(run.session_key, run.user_id, event)
        attachment_summary = gw.context_builder.attachment_summary(event)
        attachments = gw.context_builder.attachment_list(event)
        current_task = gw.memory.read_current_task(run.session_key)
        user_chunks = [
            "User request:",
            text.strip(),
        ]
        if current_task:
            user_chunks.extend(["", "Current task:", json.dumps(current_task, ensure_ascii=True, indent=2)])
        if execution_results:
            user_chunks.extend(["", "Tool results so far:", json.dumps(execution_results, ensure_ascii=True, indent=2)])
        messages: list[dict[str, Any]] = [{"role": "user", "content": "\n".join(user_chunks), "attachments": attachments}]
        if attachment_summary:
            messages.append({"role": "system", "content": attachment_summary})
        generated = await gw.model_router.generate(messages, system_prompt=system_prompt)
        return self._parse_output(generated)

    def _agent_loop_system_prompt(self, session_key: str, user_id: str, event: NormalizedInboundEvent) -> str:
        gw = self._gw
        base = gw.context_builder.system_prompt(session_key, user_id, event)
        tool_catalog = DEFAULT_CAPABILITY_REGISTRY.prompt_bundle()
        return "\n\n".join(
            [
                base,
                "Unified agent loop runtime.",
                "Return one JSON object only. No markdown, no prose outside JSON.",
                "You may either reply directly, ask for missing input, or request tool calls.",
                "Never claim you are doing an external action unless you emit tool_calls in this turn.",
                "Read skills on demand when needed. Use tools to inspect the environment or execute real work.",
                "Prefer direct chat replies when no external action is needed.",
                "Tool registry summary:",
                tool_catalog,
                "Output schemas:",
                '{"status":"reply","reply":"...","skill_ids":["optional-skill-id"]}',
                '{"status":"ask_input","prompt":"...","draft":{},"skill_ids":["optional-skill-id"]}',
                '{"status":"tool_calls","intent":"...","renderer":"default|gws.generic|memory.read|web.search|reminder.create","skill_ids":["..."],"tool_calls":[{"action":"skills.read","payload":{"skill_ids":["gws-gmail"]}}]}',
            ]
        )

    def _parse_output(self, text: str | None) -> dict[str, Any] | None:
        gw = self._gw
        payload = gw._parse_model_json(text)
        if not isinstance(payload, dict):
            return {"status": "reply", "reply": (text or "").strip()}
        status = str(payload.get("status", "")).strip().lower()
        if status in {"reply", "ask_input", "tool_calls"}:
            return payload
        if "reply" in payload:
            return {"status": "reply", "reply": str(payload.get("reply", "")).strip()}
        if "tool_calls" in payload:
            payload["status"] = "tool_calls"
            return payload
        return None

    def _requires_structured_action(
        self,
        latest_text: str,
        current_task: dict[str, Any],
        execution_results: list[dict[str, Any]],
    ) -> bool:
        gw = self._gw
        if execution_results:
            return False
        if not current_task:
            return False
        mode = str(current_task.get("mode", "")).strip().lower()
        intent = str(current_task.get("intent", "")).strip().lower()
        actions = [str(item).strip().lower() for item in current_task.get("actions", []) if str(item).strip()]
        if mode != "act" and not intent.startswith("gws.") and not any(action.startswith("gws.") for action in actions):
            return False
        lowered_user = latest_text.strip().lower()
        if not lowered_user:
            return False
        return gw.planner._is_action_follow_up(lowered_user, current_task)
