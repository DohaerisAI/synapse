from __future__ import annotations

from .models import CapabilityDecision, PlannedAction

SAFE_SHELL_COMMANDS = {"pwd", "ls", "whoami"}


class CapabilityBroker:
    def decide(self, planned_action: PlannedAction) -> CapabilityDecision:
        action = planned_action.action
        payload = planned_action.payload

        if action == "memory.read":
            return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="memory reads are local and safe")
        if action in {"self.describe", "self.health", "self.capabilities", "self.gaps", "diagnosis.report"}:
            return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="self-awareness and diagnosis actions are read-only")
        if action in {"capabilities.read", "reminder.create", "integration.propose", "integration.scaffold", "integration.test", "web.search"}:
            return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="capability reads and reminders are host-safe")
        if action == "gws.inspect":
            return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="Google Workspace help/schema inspection is host-safe")
        if action.startswith("gws."):
            requires_approval = self._gws_requires_approval(action, payload)
            reason = "send/delete Google Workspace actions require approval" if requires_approval else "inspect/read/create/update Google Workspace actions are host-safe"
            return CapabilityDecision(allowed=True, requires_approval=requires_approval, executor="host", reason=reason)
        if action == "integration.apply":
            return CapabilityDecision(allowed=True, requires_approval=True, executor="host", reason="integration apply requires approval")
        if action == "memory.write":
            scope = payload.get("scope", "session")
            if scope == "global":
                return CapabilityDecision(allowed=True, requires_approval=True, executor="host", reason="global memory writes require approval")
            return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="session and user memory writes are host-safe")
        if action == "memory.delete":
            scope = payload.get("scope", "session")
            if scope == "global":
                return CapabilityDecision(allowed=True, requires_approval=True, executor="host", reason="global memory deletes require approval")
            return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="session and user memory deletes are host-safe")
        if action in {"telegram.send", "skills.read"}:
            return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="outbound adapter sends and skill reads are host-safe")
        if action == "shell.exec":
            command = str(payload.get("command", "")).strip().split(" ", 1)[0]
            requires_approval = command not in SAFE_SHELL_COMMANDS
            return CapabilityDecision(allowed=True, requires_approval=requires_approval, executor="docker", reason="shell execution is isolated")
        if action.startswith("finance."):
            if action == "finance.trade.gtt_place":
                return CapabilityDecision(allowed=True, requires_approval=True, executor="host", reason="GTT order placement requires approval")
            return CapabilityDecision(allowed=True, requires_approval=False, executor="host", reason="finance read/analysis actions are safe")
        if action == "web.fetch":
            return CapabilityDecision(allowed=True, requires_approval=False, executor="docker", reason="web fetches should be isolated")
        if action == "code.patch.propose":
            return CapabilityDecision(allowed=True, requires_approval=True, executor="docker", reason="patch proposals require approval before generation")
        if action in {"skills.apply.proposal", "code.patch.apply"}:
            return CapabilityDecision(allowed=False, requires_approval=True, executor="none", reason="automatic patch application is disabled in MVP")
        return CapabilityDecision(allowed=False, requires_approval=True, executor="none", reason=f"unsupported capability: {action}")

    def _gws_requires_approval(self, action: str, payload: dict[str, object]) -> bool:
        if action in {
            "gws.gmail.send",
            "gws.drive.delete",
            "gws.docs.delete",
            "gws.sheets.delete",
            "gws.calendar.event.delete",
        }:
            return True
        if action.endswith(".delete"):
            return True
        if action == "gws.calendar.event.create" and bool(payload.get("attendees")):
            return True
        if action != "gws.exec":
            return False
        argv = payload.get("argv")
        if not isinstance(argv, list) or not argv:
            return True
        parts = [str(item).strip().lower() for item in argv if str(item).strip()]
        if not parts:
            return True
        if parts[0] == "schema" or "--help" in parts or "-h" in parts:
            return False
        if "send" in parts or "delete" in parts or "trash" in parts or "remove" in parts:
            return True
        if len(parts) >= 3 and parts[0] == "calendar" and parts[1] == "events" and parts[2] == "insert":
            return any("attendee" in item or "sendupdates" in item for item in parts)
        return False
