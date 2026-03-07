from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ..capabilities import DEFAULT_CAPABILITY_REGISTRY
from ..models import PlannedAction, WorkflowPlan

if TYPE_CHECKING:
    from .core import Gateway


class GWSPlanner:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    async def try_skill_planned_gws_input(self, text: str, *, session_key: str | None = None) -> dict[str, Any] | None:
        gw = self._gw
        current_task = gw.memory.read_current_task(session_key) if session_key else None
        if not self._should_try_gws_skill_planner(text, current_task):
            return None
        planned = await self.run_skill_gws_planner(text, session_key=session_key, current_task=current_task)
        if planned is None or planned.get("status") != "ask_input":
            return None
        return {
            "kind": "skill.gws",
            "payload": {
                "draft": dict(planned.get("draft", {})),
                "skill_ids": [str(item) for item in planned.get("skill_ids", [])],
                "session_key": session_key or "",
            },
            "prompt": str(planned.get("prompt", "I need a bit more information for that Google Workspace request.")).strip(),
        }

    async def try_skill_planned_gws_workflow(self, text: str, *, session_key: str | None = None) -> WorkflowPlan | None:
        gw = self._gw
        current_task = gw.memory.read_current_task(session_key) if session_key else None
        if not self._should_try_gws_skill_planner(text, current_task):
            return None
        planned = await self.run_skill_gws_planner(text, session_key=session_key, current_task=current_task)
        if planned is None or planned.get("status") != "workflow":
            return None
        actions_payload = planned.get("actions", [])
        if not isinstance(actions_payload, list) or not actions_payload:
            return None
        actions = [PlannedAction.from_dict(item) for item in actions_payload if isinstance(item, dict)]
        if not actions:
            return None
        return gw._workflow_from_actions(
            str(planned.get("intent", "gws.skill")),
            actions,
            renderer=str(planned.get("renderer", "gws.generic")),
            skill_ids=[str(item) for item in planned.get("skill_ids", [])],
        )

    async def run_skill_gws_planner(
        self,
        text: str,
        *,
        draft: dict[str, Any] | None = None,
        skill_ids: list[str] | None = None,
        session_key: str | None = None,
        current_task: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        gw = self._gw
        inherited_skill_ids: list[str] = []
        if current_task is None and session_key:
            current_task = gw.memory.read_current_task(session_key)
        if isinstance(current_task, dict):
            inherited_skill_ids = [str(item) for item in current_task.get("skill_ids", []) if str(item).strip()]
        candidate_ids = list(skill_ids or gw.skills.select_candidates(text, capability="gws") or inherited_skill_ids)
        if not candidate_ids:
            return None
        skill_index = gw.skills.index_bundle(candidate_ids)
        skill_docs = gw.skills.read(candidate_ids)
        skill_ops = gw.memory.read_skill_operations().strip()
        if current_task is None and isinstance(draft, dict):
            current_task = draft.get("current_task") if isinstance(draft.get("current_task"), dict) else None
        playbook_ids = gw.workspace.select_playbooks(text)
        playbook_docs = gw.workspace.read_playbooks(playbook_ids)
        draft_json = json.dumps(draft or {}, ensure_ascii=True, indent=2)
        user_message = "\n".join(
            [
                f"User request:\n{text.strip()}",
                "",
                "Candidate skills:",
                skill_index or "(none)",
                "",
                "Loaded skill instructions:",
                skill_docs or "(none)",
                "",
                "Operational memory:",
                skill_ops or "(none)",
                "",
                "Current task:",
                json.dumps(current_task, ensure_ascii=True, indent=2) if current_task else "(none)",
                "",
                "Relevant playbooks:",
                playbook_docs or "(none)",
                "",
                "Existing draft:",
                draft_json,
            ]
        )
        system_prompt = "\n".join(
            [
                "Google Workspace planning runtime.",
                "Return one JSON object only. No markdown, no prose.",
                "Decide whether the request is a Google Workspace task that should use the gws CLI and the loaded skills.",
                "Use the loaded SKILL.md instructions to decide what to run.",
                "Use optimized built-in actions when they fit, otherwise use gws.inspect or gws.exec.",
                "Use gws.inspect for help/schema discovery only, such as `gws gmail --help` or `gws schema gmail.users.messages.list`.",
                "Use gws.exec for real gws CLI commands when a built-in action does not cover the request.",
                "Never invent shell commands outside gws.inspect or gws.exec.",
                "If required inputs are missing, ask only for the missing fields and return status ask_input.",
                "Calendar events with attendees count as sending invites; plain internal reads and normal create/update writes do not need approval.",
                "For gws.calendar.event.create, start and end must be RFC3339 timestamps with timezone offsets, never natural-language phrases.",
                "If the user says things like 'tomorrow 5pm' or gives no timezone, ask for or infer the missing pieces before emitting the final action.",
                "Supported runtime capabilities:",
                DEFAULT_CAPABILITY_REGISTRY.prompt_bundle(
                    actions=[
                        "gws.auth.status",
                        "gws.gmail.latest",
                        "gws.gmail.search",
                        "gws.gmail.get",
                        "gws.gmail.triage",
                        "gws.gmail.send",
                        "gws.calendar.agenda",
                        "gws.workflow.meeting.prep",
                        "gws.calendar.event.create",
                        "gws.drive.search",
                        "gws.drive.upload",
                        "gws.drive.text.create",
                        "gws.docs.create",
                        "gws.docs.write",
                        "gws.sheets.create",
                        "gws.sheets.read",
                        "gws.sheets.append",
                        "gws.inspect",
                        "gws.exec",
                    ]
                ),
                "Output schema:",
                '{"status":"workflow|ask_input|not_gws","intent":"...", "renderer":"gws.generic", "skill_ids":["..."], "prompt":"...", "draft":{}, "actions":[{"action":"gws.exec","payload":{"argv":["gmail","--help"],"service":"gmail"}}]}',
            ]
        )
        if gw.gws_planner_instructions:
            system_prompt = system_prompt + "\n\nPlanner-specific runtime instructions:\n" + gw.gws_planner_instructions
        generated = await gw.model_router.generate([{"role": "user", "content": user_message}], system_prompt=system_prompt)
        payload = gw._parse_model_json(generated)
        if not isinstance(payload, dict):
            return None
        payload["skill_ids"] = [str(item) for item in payload.get("skill_ids", candidate_ids) if str(item).strip()]
        status = str(payload.get("status", "")).strip().lower()
        if status not in {"workflow", "ask_input", "not_gws"}:
            return None
        return payload

    def _should_try_gws_skill_planner(self, text: str, current_task: dict[str, Any] | None) -> bool:
        if self.looks_like_gws_request(text):
            return True
        if not current_task:
            return False
        skill_ids = [str(item) for item in current_task.get("skill_ids", []) if str(item).strip()]
        actions = [str(item) for item in current_task.get("actions", []) if str(item).strip()]
        intent = str(current_task.get("intent", "")).strip().lower()
        if any(skill_id.startswith("gws-") for skill_id in skill_ids):
            return True
        if any(action.startswith("gws.") for action in actions):
            return True
        return intent.startswith("gws.")

    def looks_like_gws_request(self, text: str) -> bool:
        lowered = text.lower().strip()
        if lowered.startswith("/gws "):
            return True
        markers = (
            "gmail",
            "email",
            "mail",
            "calendar",
            "meeting",
            "drive",
            "doc",
            "document",
            "sheet",
            "spreadsheet",
            "workspace",
            "google",
        )
        return any(marker in lowered for marker in markers)

    def gws_workflow(self, intent: str, actions: list[PlannedAction], *, renderer: str) -> WorkflowPlan:
        gw = self._gw
        prepared: list[PlannedAction] = []
        for action in actions:
            preview = gw.host_executor.gws.preview_action(action.action, action.payload)
            payload = dict(action.payload)
            payload["command_preview"] = preview
            prepared.append(PlannedAction(action=action.action, payload=payload))
        return gw._workflow(intent, prepared, renderer=renderer)
