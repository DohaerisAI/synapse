from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..attachments import attachment_prompt_context
from ..models import NormalizedInboundEvent

if TYPE_CHECKING:
    from .core import Gateway


_BASE_INSTRUCTIONS = [
    "Be concise — replies go to Telegram chat.",
    "Use memory and session context when relevant, but never claim to remember something unless it appears in the provided context.",
    "If the user asks for live information: use your tools to fetch it. Only say you cannot if the tool call itself fails.",
    "Do not enumerate capabilities unless asked.",
]


class ContextBuilder:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    def _identity_lines(self) -> list[str]:
        gw = self._gw
        return [
            f"You are {gw.agent_name}, a direct personal assistant for the user.",
            f"Your name is {gw.agent_name}.",
            f"If the user asks your name, answer with exactly {gw.agent_name} unless they ask for more detail.",
        ]

    def _heartbeat_chunks(self, event: NormalizedInboundEvent) -> list[str]:
        gw = self._gw
        chunks: list[str] = []
        if not self.is_heartbeat(event):
            return chunks
        heartbeat_policy = ""
        if gw.heartbeat_path and gw.heartbeat_path.exists():
            heartbeat_policy = gw.heartbeat_path.read_text(encoding="utf-8").strip()
        chunks.append(
            "\n".join(
                [
                    "You are running a proactive heartbeat.",
                    "Review stored memory and recent transcript for anything the user should be reminded of or asked about.",
                    "If there is nothing meaningful to surface, return exactly HEARTBEAT_OK.",
                    "If something matters, return one concise actionable message only.",
                ]
            )
        )
        if heartbeat_policy:
            chunks.append("Heartbeat policy:\n\n" + heartbeat_policy)
        return chunks

    def system_prompt(self, session_key: str, user_id: str, event: NormalizedInboundEvent) -> str:
        gw = self._gw
        capability_summary = gw.skills.capability_bundle() or ""
        workspace_context = gw.workspace.context_bundle(session_key, user_id)
        chunks = [
            "\n".join(
                self._identity_lines()
                + _BASE_INSTRUCTIONS
                + [
                    "Decide first whether the user wants conversation/thinking or a real-world action.",
                    "If conversation, drafting, brainstorming, rewriting, or explanation is enough, answer directly in chat.",
                    "Use external capabilities only when the task requires fetching, saving, creating, editing, sending, deleting, or otherwise touching the world outside the chat.",
                    "Do not let domain words alone trigger execution.",
                    "Latest/current/news-style questions may be routed through a Codex CLI-backed web search helper before you answer.",
                    "If an action is pending approval, say it is pending; do not imply it already happened.",
                    "Rely on the capability summary, skill index, and playbooks to decide what exists; read detailed skills only when needed.",
                    "Only outward or destructive actions should require approval. Reads, inspect commands, and ordinary create/update writes can run directly when policy allows.",
                    "If the user replies with yes/approve/go ahead while a request is waiting on approval, treat it as approval. If they reply with no/cancel/stop, treat it as rejection.",
                    "Do not say you are sending, checking, fetching, creating, or verifying something unless this turn actually entered execution, created a real approval, or asked for specific missing input.",
                    "For follow-ups on an active task, either act, ask for the exact missing detail, or stay clearly conversational; do not make empty promises about future actions.",
                    "You can remove memory with /forget-session, /forget-user, /forget-global, or 'forget that ...' for user memory.",
                    "Heartbeat is a periodic proactive review, not the same thing as a reminder timer. Use reminder features for time-based follow-ups.",
                ]
            ),
            capability_summary,
        ]
        if gw.assistant_instructions:
            chunks.append("Runtime-specific assistant instructions:\n\n" + gw.assistant_instructions)
        chunks.extend(self._heartbeat_chunks(event))
        if workspace_context:
            chunks.append("Use this workspace, memory, and recent context when responding:\n\n" + workspace_context)
        return "\n\n".join(chunks)

    def react_system_prompt(self, session_key: str, user_id: str, event: NormalizedInboundEvent) -> str:
        """System prompt for the ReAct tool-calling path.

        Omits the capability registry family bundle (tools ARE the capabilities)
        and adds compact skill index + tool usage instructions instead.
        """
        gw = self._gw
        workspace_context = gw.workspace.context_bundle(session_key, user_id)
        chunks = [
            "\n".join(
                self._identity_lines()
                + _BASE_INSTRUCTIONS
                + [
                    "Call tools directly to accomplish tasks. Do not describe actions — just do them.",
                    "If the task is purely conversational, answer directly without calling tools.",
                    "When a task matches a skill: load_skill → read instructions → shell_exec. Never skip execution or assume failure.",
                    "NEVER claim a tool call failed unless you actually called it and got an error.",
                    "Return NO_REPLY for heartbeats with nothing to surface.",
                ]
            ),
        ]
        # Compact skill index
        skill_index = gw.skills.index_bundle() if hasattr(gw.skills, "index_bundle") else gw.skills.capability_bundle()
        if skill_index:
            chunks.append(
                "Available skills (call load_skill to get full instructions):\n"
                + skill_index
                + "\n\n"
                "To use a skill: load_skill('<id>') → read the commands → shell_exec('<command>'). "
                "Examples: load_skill('gws-gmail') → shell_exec('gws gmail +triage'), "
                "load_skill('swing-trader') → shell_exec('python3 <path>/scripts/scanner.py scan --pattern all')."
            )
        if gw.assistant_instructions:
            chunks.append("Runtime-specific assistant instructions:\n\n" + gw.assistant_instructions)
        chunks.extend(self._heartbeat_chunks(event))
        if workspace_context:
            chunks.append("Use this workspace, memory, and recent context when responding:\n\n" + workspace_context)
        return "\n\n".join(chunks)

    def is_heartbeat(self, event: NormalizedInboundEvent) -> bool:
        return str(event.metadata.get("kind", "")).strip().lower() == "heartbeat"

    def attachment_summary(self, event: NormalizedInboundEvent) -> str:
        normalized = self.attachment_list(event)
        if not normalized:
            return ""
        return "Inbound attachments:\n" + attachment_prompt_context(normalized)

    def attachment_list(self, event: NormalizedInboundEvent) -> list[dict[str, Any]]:
        attachments = event.metadata.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return []
        return [item for item in attachments if isinstance(item, dict)]
