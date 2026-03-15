from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ..models import NormalizedInboundEvent, PlannedAction, WorkflowPlan

if TYPE_CHECKING:
    from .core import Gateway


class WorkflowPlanner:
    """Deterministic planner for slash commands only.

    All LLM-driven routing is now handled by the ReAct loop.
    """

    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    async def plan_workflow(self, event: NormalizedInboundEvent, *, session_key: str | None = None) -> WorkflowPlan:
        gw = self._gw
        text = event.text.strip()
        lowered = text.lower()

        # Memory commands
        if lowered in {"/memory", "/what-do-you-remember"}:
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

        # Help
        if lowered == "/help":
            return gw._workflow("capabilities.read", [PlannedAction(action="capabilities.read", payload={})], renderer="capabilities.read")

        # Web search
        if text.startswith("/search "):
            return gw._workflow("web.search", [PlannedAction(action="web.search", payload={"query": text.removeprefix("/search ").strip()})], renderer="web.search")

        # Shell
        if text.startswith("/shell "):
            return gw._workflow("shell.exec", [PlannedAction(action="shell.exec", payload={"command": text.removeprefix("/shell ").strip()})])

        # Fetch
        if text.startswith("/fetch "):
            return gw._workflow("web.fetch", [PlannedAction(action="web.fetch", payload={"url": text.removeprefix("/fetch ").strip()})])

        # Patch proposal
        if text.startswith("/propose-patch "):
            return gw._workflow("code.patch.propose", [PlannedAction(action="code.patch.propose", payload={"instructions": text.removeprefix("/propose-patch ").strip()})])

        # GWS slash commands
        if lowered.startswith("/gws "):
            gws_workflow = self._parse_gws_slash(text)
            if gws_workflow is not None:
                return gws_workflow

        # Fallback: empty workflow (will be handled by react loop)
        return gw._workflow("chat.respond", [], renderer="default")

    def _parse_gws_slash(self, text: str) -> WorkflowPlan | None:
        """Parse /gws <subcommand> into a workflow."""
        gw = self._gw
        remainder = text[5:].strip()
        lowered = remainder.lower()

        if lowered == "status":
            return gw._workflow("gws.auth.status", [PlannedAction(action="gws.auth.status", payload={})], renderer="gws.auth.status")
        if lowered == "auth setup":
            return gw._workflow("gws.auth.setup", [PlannedAction(action="gws.auth.setup", payload={})], renderer="gws.auth.status")
        if lowered == "auth login":
            return gw._workflow("gws.auth.login", [PlannedAction(action="gws.auth.login", payload={})], renderer="gws.auth.status")
        if lowered == "gmail latest":
            return gw._workflow("gws.gmail.latest", [PlannedAction(action="gws.gmail.latest", payload={})], renderer="gws.gmail.latest")
        if lowered.startswith("gmail search "):
            return gw._workflow("gws.gmail.search", [PlannedAction(action="gws.gmail.search", payload={"query": remainder[13:].strip()})], renderer="gws.gmail.search")
        if lowered.startswith("gmail send "):
            parts = [part.strip() for part in remainder[11:].split("|", 2)]
            if len(parts) == 3:
                return gw._workflow("gws.gmail.send", [PlannedAction(action="gws.gmail.send", payload={"to": parts[0], "subject": parts[1], "body": parts[2]})], renderer="gws.gmail.send")
            return None
        if lowered.startswith("calendar agenda"):
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
            return gw._workflow("gws.calendar.agenda", [PlannedAction(action="gws.calendar.agenda", payload=payload)], renderer="gws.calendar.agenda")
        if lowered.startswith("calendar create "):
            parts = [part.strip() for part in remainder[16:].split("|")]
            if len(parts) >= 3:
                payload = {"summary": parts[0], "start": parts[1], "end": parts[2]}
                if len(parts) >= 4 and parts[3]:
                    payload["timezone"] = parts[3]
                if len(parts) >= 5 and parts[4]:
                    payload["attendees"] = [item.strip() for item in parts[4].split(",") if item.strip()]
                return gw._workflow("gws.calendar.event.create", [PlannedAction(action="gws.calendar.event.create", payload=payload)], renderer="gws.calendar.create")
            return None
        if lowered.startswith("drive search "):
            return gw._workflow("gws.drive.search", [PlannedAction(action="gws.drive.search", payload={"query": remainder[13:].strip()})], renderer="gws.drive.search")
        if lowered.startswith("drive upload "):
            parts = [part.strip() for part in remainder[13:].split("|", 2)]
            payload = {"path": parts[0]}
            if len(parts) > 1 and parts[1]:
                payload["name"] = parts[1]
            if len(parts) > 2 and parts[2]:
                payload["parent_id"] = parts[2]
            return gw._workflow("gws.drive.upload", [PlannedAction(action="gws.drive.upload", payload=payload)], renderer="gws.drive.upload")
        if lowered.startswith("drive create text "):
            parts = [part.strip() for part in remainder[18:].split("|", 2)]
            if len(parts) >= 2:
                payload = {"name": parts[0], "text": parts[1]}
                if len(parts) >= 3 and parts[2]:
                    payload["parent_id"] = parts[2]
                return gw._workflow("gws.drive.text.create", [PlannedAction(action="gws.drive.text.create", payload=payload)], renderer="gws.drive.upload")
            return None
        if lowered.startswith("docs create "):
            return gw._workflow("gws.docs.create", [PlannedAction(action="gws.docs.create", payload={"name": remainder[12:].strip()})], renderer="gws.docs.create")
        if lowered.startswith("docs write "):
            parts = [part.strip() for part in remainder[11:].split("|", 1)]
            if len(parts) == 2:
                return gw._workflow("gws.docs.write", [PlannedAction(action="gws.docs.write", payload={"document_id": parts[0], "text": parts[1]})], renderer="gws.docs.write")
            return None
        if lowered.startswith("sheets create "):
            return gw._workflow("gws.sheets.create", [PlannedAction(action="gws.sheets.create", payload={"title": remainder[14:].strip()})], renderer="gws.sheets.create")
        if lowered.startswith("sheets read "):
            parts = [part.strip() for part in remainder[12:].split("|", 1)]
            if len(parts) == 2:
                return gw._workflow("gws.sheets.read", [PlannedAction(action="gws.sheets.read", payload={"spreadsheet_id": parts[0], "range": parts[1]})], renderer="gws.sheets.read")
            return None
        if lowered.startswith("sheets append "):
            parts = [part.strip() for part in remainder[14:].split("|", 2)]
            if len(parts) != 3:
                return None
            try:
                values = json.loads(parts[2])
            except json.JSONDecodeError:
                return None
            return gw._workflow("gws.sheets.append", [PlannedAction(action="gws.sheets.append", payload={"spreadsheet_id": parts[0], "range": parts[1], "values": values})], renderer="gws.sheets.append")
        return None
