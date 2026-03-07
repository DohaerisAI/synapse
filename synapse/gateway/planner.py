from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ..identifiers import derive_session_key
from ..models import NormalizedInboundEvent, PlannedAction, WorkflowPlan

if TYPE_CHECKING:
    from .core import Gateway


class WorkflowPlanner:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    async def plan_workflow(self, event: NormalizedInboundEvent, *, session_key: str | None = None) -> WorkflowPlan:
        gw = self._gw
        text = event.text.strip()
        lowered = text.lower()
        if lowered in {"/memory", "/what-do-you-remember", "what do you remember about me"}:
            return gw._workflow("memory.read", [PlannedAction(action="memory.read", payload={"scope": "all"})], renderer="memory.read")
        if text.startswith("/remember-session "):
            return gw._workflow("memory.write.session", [PlannedAction(action="memory.write", payload={"scope": "session", "content": text.removeprefix("/remember-session ").strip()})])
        if text.startswith("/remember-user "):
            return gw._workflow("memory.write.user", [PlannedAction(action="memory.write", payload={"scope": "user", "content": text.removeprefix("/remember-user ").strip()})])
        if text.startswith("/remember-global "):
            return gw._workflow("memory.write.global", [PlannedAction(action="memory.write", payload={"scope": "global", "content": text.removeprefix("/remember-global ").strip()})])
        if text.startswith("/forget-session "):
            return gw._workflow("memory.delete.session", [PlannedAction(action="memory.delete", payload={"scope": "session", "content": text.removeprefix("/forget-session ").strip()})])
        if text.startswith("/forget-user "):
            return gw._workflow("memory.delete.user", [PlannedAction(action="memory.delete", payload={"scope": "user", "content": text.removeprefix("/forget-user ").strip()})])
        if text.startswith("/forget-global "):
            return gw._workflow("memory.delete.global", [PlannedAction(action="memory.delete", payload={"scope": "global", "content": text.removeprefix("/forget-global ").strip()})])
        if lowered in {"/help", "help", "what can you do", "what are your features", "what files do you support"}:
            return gw._workflow("capabilities.read", [PlannedAction(action="capabilities.read", payload={})], renderer="capabilities.read")
        if text.startswith("/search "):
            return gw._workflow("web.search", [PlannedAction(action="web.search", payload={"query": text.removeprefix("/search ").strip()})], renderer="web.search")
        integration_request = gw.extractors.extract_integration_request(text)
        if integration_request is not None:
            integration_id = integration_request["integration_id"]
            return gw._workflow(
                f"integration.{integration_id}",
                [
                    PlannedAction(action="integration.propose", payload=integration_request),
                    PlannedAction(action="integration.scaffold", payload={"integration_id": integration_id}),
                    PlannedAction(action="integration.test", payload={"integration_id": integration_id}),
                    PlannedAction(action="integration.apply", payload={"integration_id": integration_id}),
                ],
                renderer="integration",
            )
        if lowered.startswith("remember that "):
            return gw._workflow("memory.write.user", [PlannedAction(action="memory.write", payload={"scope": "user", "content": text[14:].strip()})])
        if lowered.startswith("remember this about me:"):
            return gw._workflow("memory.write.user", [PlannedAction(action="memory.write", payload={"scope": "user", "content": text.split(":", 1)[1].strip()})])
        if lowered.startswith("forget that "):
            return gw._workflow("memory.delete.user", [PlannedAction(action="memory.delete", payload={"scope": "user", "content": text[12:].strip()})])
        reminder = gw.extractors.extract_reminder_request(event)
        if reminder is not None:
            return gw._workflow("reminder.create", [PlannedAction(action="reminder.create", payload=reminder)], renderer="reminder.create")
        preference = gw.extractors.extract_user_preference(text)
        if preference:
            return gw._workflow("memory.write.user", [PlannedAction(action="memory.write", payload={"scope": "user", "content": preference})])
        if text.startswith("/shell "):
            return gw._workflow("shell.exec", [PlannedAction(action="shell.exec", payload={"command": text.removeprefix("/shell ").strip()})])
        if text.startswith("/fetch "):
            return gw._workflow("web.fetch", [PlannedAction(action="web.fetch", payload={"url": text.removeprefix("/fetch ").strip()})])
        if text.startswith("/propose-patch "):
            return gw._workflow("code.patch.propose", [PlannedAction(action="code.patch.propose", payload={"instructions": text.removeprefix("/propose-patch ").strip()})])
        if await self._intent_mode(event, session_key=session_key) == "act":
            gws_workflow = await self._extract_gws_workflow(text, session_key=session_key)
            if gws_workflow is not None:
                return gws_workflow
        if self._should_use_web_search(text):
            return gw._workflow("web.search", [PlannedAction(action="web.search", payload={"query": text})], renderer="web.search")
        return gw._workflow("chat.respond", [], renderer="default")

    async def plan_pending_input(self, event: NormalizedInboundEvent, *, session_key: str | None = None) -> dict[str, Any] | None:
        gw = self._gw
        text = event.text.strip()
        if await self._intent_mode(event, session_key=session_key) != "act":
            return None
        planned = await gw.gws_planner.try_skill_planned_gws_input(text, session_key=session_key)
        if planned is not None:
            return planned
        return None

    async def _intent_mode(self, event: NormalizedInboundEvent, *, session_key: str | None = None) -> str:
        gw = self._gw
        cached = str(event.metadata.get("_intent_mode", "")).strip().lower()
        if cached in {"chat", "act"}:
            return cached
        lowered = event.text.strip().lower()
        if lowered.startswith("/gws "):
            event.metadata["_intent_mode"] = "act"
            return "act"
        resolved_session_key = session_key or derive_session_key(event.adapter, event.channel_id, event.user_id)
        current_task = gw.memory.read_current_task(resolved_session_key)
        if self._is_action_follow_up(event.text, current_task):
            event.metadata["_intent_mode"] = "act"
            return "act"
        mode = await self._run_intent_router(event.text, current_task=current_task)
        event.metadata["_intent_mode"] = mode
        return mode

    async def _run_intent_router(self, text: str, *, current_task: dict[str, Any] | None = None) -> str:
        gw = self._gw
        capability_summary = gw.skills.capability_bundle() or "Capabilities are available through skills and integrations."
        system_prompt = "\n".join(
            [
                "Intent routing runtime.",
                "Return one JSON object only. No markdown, no prose.",
                "Choose whether the user wants normal conversation or a real-world action.",
                'Use mode=\"chat\" for drafting, brainstorming, explaining, rewriting, advising, or showing a draft.',
                'Use mode=\"act\" only when the user wants you to read, fetch, create, update, save, send, delete, verify, or otherwise touch an external system or durable state.',
                "Use the current task context when the new message is a short follow-up like yes, no, do it, send it, check it, show proof, make it shorter, or change the ending.",
                "Do not let domain nouns like email, calendar, docs, or sheets decide by themselves.",
                "If the current task shows the user is continuing toward a real action, classify as act even if the latest message alone is underspecified.",
                "If the current task shows the user is still iterating on wording or ideas, classify as chat.",
                "Do not choose chat if that would only produce promises about future actions without acting.",
                "Examples:",
                '- "draft a mail for Apoorva" -> {"mode":"chat"}',
                '- "show me the draft first" -> {"mode":"chat"}',
                '- "tell me about yourself" -> {"mode":"chat"}',
                '- "fetch me my last mail" -> {"mode":"act"}',
                '- "what is on my calendar today" -> {"mode":"act"}',
                '- "create a sheet and add this row" -> {"mode":"act"}',
                '- current task says "reply yes and I will send it", new message "yes please" -> {"mode":"act"}',
                '- current task says "I can verify it by checking", new message "show me proof" -> {"mode":"act"}',
                "Output schema:",
                '{"mode":"chat|act"}',
            ]
        )
        user_message = "\n".join(
            [
                "Capability summary:",
                capability_summary,
                "",
                "Current task:",
                json.dumps(current_task, ensure_ascii=True, indent=2) if current_task else "(none)",
                "",
                "User request:",
                text.strip(),
            ]
        )
        generated = await gw.model_router.generate([{"role": "user", "content": user_message}], system_prompt=system_prompt)
        payload = gw._parse_model_json(generated)
        if not isinstance(payload, dict):
            return "chat"
        mode = str(payload.get("mode", "")).strip().lower()
        return mode if mode in {"chat", "act"} else "chat"

    def _is_action_follow_up(self, text: str, current_task: dict[str, Any] | None) -> bool:
        if not current_task:
            return False
        operational = self._is_operational_task(current_task)
        if not operational:
            return False
        lowered = text.strip().lower()
        if not lowered:
            return False
        if self._gw.approval_handler.is_approval_confirmation(lowered):
            return True
        continuations = {
            "send it",
            "send now",
            "do it",
            "go ahead",
            "check it",
            "check now",
            "show proof",
            "show me proof",
            "done?",
            "is it done",
            "did it send",
        }
        if lowered in continuations:
            return True
        if len(lowered.split()) <= 4 and lowered.endswith("?"):
            return True
        return False

    def _is_operational_task(self, current_task: dict[str, Any] | None) -> bool:
        if not current_task:
            return False
        mode = str(current_task.get("mode", "")).strip().lower()
        if mode == "act":
            return True
        intent = str(current_task.get("intent", "")).strip().lower()
        if intent and intent != "chat.respond":
            return True
        actions = [str(item).strip() for item in current_task.get("actions", []) if str(item).strip()]
        return bool(actions)

    def _should_use_web_search(self, text: str) -> bool:
        lowered = text.lower().strip()
        if not lowered:
            return False
        if self._gw.gws_planner.looks_like_gws_request(text):
            return False
        markers = (
            "latest",
            "current",
            "today",
            "news",
            "recent",
            "right now",
            "up to date",
            "up-to-date",
        )
        if any(marker in lowered for marker in markers):
            return True
        return lowered.startswith(("what's happening", "whats happening"))

    async def _extract_gws_workflow(self, text: str, *, session_key: str | None = None) -> WorkflowPlan | None:
        gw = self._gw
        stripped = text.strip()
        lowered = stripped.lower()
        if lowered in {"setup google workspace", "configure google workspace", "connect google workspace", "google workspace status", "show gws auth status"}:
            return gw.gws_planner.gws_workflow("gws.auth.status", [PlannedAction(action="gws.auth.status", payload={})], renderer="gws.auth.status")
        if not lowered.startswith("/gws "):
            return await gw.gws_planner.try_skill_planned_gws_workflow(stripped, session_key=session_key)
        remainder = stripped[5:].strip()
        lowered_remainder = remainder.lower()
        if lowered_remainder == "status":
            return gw.gws_planner.gws_workflow("gws.auth.status", [PlannedAction(action="gws.auth.status", payload={})], renderer="gws.auth.status")
        if lowered_remainder == "auth setup":
            return gw.gws_planner.gws_workflow("gws.auth.setup", [PlannedAction(action="gws.auth.setup", payload={})], renderer="gws.auth.status")
        if lowered_remainder == "auth login":
            return gw.gws_planner.gws_workflow("gws.auth.login", [PlannedAction(action="gws.auth.login", payload={})], renderer="gws.auth.status")
        if lowered_remainder == "gmail latest":
            return gw.gws_planner.gws_workflow("gws.gmail.latest", [PlannedAction(action="gws.gmail.latest", payload={})], renderer="gws.gmail.latest")
        if lowered_remainder.startswith("gmail search "):
            return gw.gws_planner.gws_workflow("gws.gmail.search", [PlannedAction(action="gws.gmail.search", payload={"query": remainder[13:].strip()})], renderer="gws.gmail.search")
        if lowered_remainder.startswith("gmail send "):
            parts = [part.strip() for part in remainder[11:].split("|", 2)]
            if len(parts) == 3:
                return gw.gws_planner.gws_workflow("gws.gmail.send", [PlannedAction(action="gws.gmail.send", payload={"to": parts[0], "subject": parts[1], "body": parts[2]})], renderer="gws.gmail.send")
            return None
        if lowered_remainder.startswith("calendar agenda"):
            tail = remainder[15:].strip()
            payload: dict[str, Any] = {}
            if tail == "today":
                payload["today"] = True
            elif tail == "tomorrow":
                payload["tomorrow"] = True
            elif tail == "week":
                payload["week"] = True
            elif tail.isdigit():
                payload["days"] = int(tail)
            return gw.gws_planner.gws_workflow("gws.calendar.agenda", [PlannedAction(action="gws.calendar.agenda", payload=payload)], renderer="gws.calendar.agenda")
        if lowered_remainder.startswith("calendar create "):
            parts = [part.strip() for part in remainder[16:].split("|")]
            if len(parts) >= 3:
                payload: dict[str, Any] = {"summary": parts[0], "start": parts[1], "end": parts[2]}
                if len(parts) >= 4 and parts[3]:
                    payload["timezone"] = parts[3]
                if len(parts) >= 5 and parts[4]:
                    payload["attendees"] = [item.strip() for item in parts[4].split(",") if item.strip()]
                return gw.gws_planner.gws_workflow("gws.calendar.event.create", [PlannedAction(action="gws.calendar.event.create", payload=payload)], renderer="gws.calendar.create")
            return None
        if lowered_remainder.startswith("drive search "):
            return gw.gws_planner.gws_workflow("gws.drive.search", [PlannedAction(action="gws.drive.search", payload={"query": remainder[13:].strip()})], renderer="gws.drive.search")
        if lowered_remainder.startswith("drive upload "):
            parts = [part.strip() for part in remainder[13:].split("|", 2)]
            payload = {"path": parts[0]}
            if len(parts) > 1 and parts[1]:
                payload["name"] = parts[1]
            if len(parts) > 2 and parts[2]:
                payload["parent_id"] = parts[2]
            return gw.gws_planner.gws_workflow("gws.drive.upload", [PlannedAction(action="gws.drive.upload", payload=payload)], renderer="gws.drive.upload")
        if lowered_remainder.startswith("drive create text "):
            parts = [part.strip() for part in remainder[18:].split("|", 2)]
            if len(parts) >= 2:
                payload = {"name": parts[0], "text": parts[1]}
                if len(parts) >= 3 and parts[2]:
                    payload["parent_id"] = parts[2]
                return gw.gws_planner.gws_workflow("gws.drive.text.create", [PlannedAction(action="gws.drive.text.create", payload=payload)], renderer="gws.drive.upload")
            return None
        if lowered_remainder.startswith("docs create "):
            return gw.gws_planner.gws_workflow("gws.docs.create", [PlannedAction(action="gws.docs.create", payload={"name": remainder[12:].strip()})], renderer="gws.docs.create")
        if lowered_remainder.startswith("docs write "):
            parts = [part.strip() for part in remainder[11:].split("|", 1)]
            if len(parts) == 2:
                return gw.gws_planner.gws_workflow("gws.docs.write", [PlannedAction(action="gws.docs.write", payload={"document_id": parts[0], "text": parts[1]})], renderer="gws.docs.write")
            return None
        if lowered_remainder.startswith("sheets create "):
            return gw.gws_planner.gws_workflow("gws.sheets.create", [PlannedAction(action="gws.sheets.create", payload={"title": remainder[14:].strip()})], renderer="gws.sheets.create")
        if lowered_remainder.startswith("sheets read "):
            parts = [part.strip() for part in remainder[12:].split("|", 1)]
            if len(parts) == 2:
                return gw.gws_planner.gws_workflow("gws.sheets.read", [PlannedAction(action="gws.sheets.read", payload={"spreadsheet_id": parts[0], "range": parts[1]})], renderer="gws.sheets.read")
            return None
        if lowered_remainder.startswith("sheets append "):
            parts = [part.strip() for part in remainder[14:].split("|", 2)]
            if len(parts) != 3:
                return None
            try:
                values = json.loads(parts[2])
            except json.JSONDecodeError:
                return None
            return gw.gws_planner.gws_workflow("gws.sheets.append", [PlannedAction(action="gws.sheets.append", payload={"spreadsheet_id": parts[0], "range": parts[1], "values": values})], renderer="gws.sheets.append")
        return None
