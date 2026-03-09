from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ..capabilities import DEFAULT_CAPABILITY_REGISTRY
from ..models import PlannedAction, WorkflowPlan

if TYPE_CHECKING:
    from .core import Gateway

# Families handled by dedicated planners (GWS) — skip here.
_EXCLUDED_FAMILIES = {"gws"}


class ActionPlanner:
    """LLM-driven planner for non-GWS capabilities.

    Given a user request classified as 'act', asks the model which
    capability actions to invoke and with what payloads.  The model
    sees the full capability registry (minus GWS, which has its own
    planner) and returns structured JSON.
    """

    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    async def try_plan(self, text: str, *, session_key: str | None = None) -> WorkflowPlan | None:
        gw = self._gw
        current_task = gw.memory.read_current_task(session_key) if session_key else None
        planned = await self._run_action_planner(text, current_task=current_task)
        if planned is None:
            return None
        status = str(planned.get("status", "")).strip().lower()
        if status != "workflow":
            return None
        actions_raw = planned.get("actions", [])
        if not isinstance(actions_raw, list) or not actions_raw:
            return None
        actions = [PlannedAction.from_dict(item) for item in actions_raw if isinstance(item, dict)]
        if not actions:
            return None
        return gw._workflow_from_actions(
            str(planned.get("intent", "action")),
            actions,
            renderer=str(planned.get("renderer", "default")),
        )

    async def _run_action_planner(self, text: str, *, current_task: dict[str, Any] | None = None) -> dict[str, Any] | None:
        gw = self._gw
        capability_prompt = self._capability_prompt()
        if not capability_prompt:
            return None
        user_message = "\n".join([
            f"User request:\n{text.strip()}",
            "",
            "Current task:",
            json.dumps(current_task, ensure_ascii=True, indent=2) if current_task else "(none)",
        ])
        system_prompt = "\n".join([
            "Action planning runtime.",
            "Return one JSON object only. No markdown, no prose.",
            "Decide whether the user request maps to one or more of the available runtime actions below.",
            "If the request is a match, return status=workflow with the actions to execute.",
            "If the request does not match any available action, return status=no_match.",
            "Each action must include an 'action' key (the action id) and a 'payload' dict with the required arguments.",
            "You may chain multiple actions in sequence when needed.",
            "",
            "Available actions:",
            capability_prompt,
            "",
            "Output schema:",
            '{"status":"workflow|no_match", "intent":"<short intent label>", "renderer":"default", "actions":[{"action":"<action_id>","payload":{...}}]}',
        ])
        generated = await gw.model_router.generate(
            [{"role": "user", "content": user_message}],
            system_prompt=system_prompt,
        )
        payload = gw._parse_model_json(generated)
        if not isinstance(payload, dict):
            return None
        return payload

    def _capability_prompt(self) -> str:
        lines = []
        for defn in DEFAULT_CAPABILITY_REGISTRY._definitions:
            if defn.family in _EXCLUDED_FAMILIES:
                continue
            if not defn.prompt_visible:
                continue
            args = f" {defn.args_hint}" if defn.args_hint else ""
            lines.append(f"- {defn.action}{args}: {defn.description}")
        return "\n".join(lines)
