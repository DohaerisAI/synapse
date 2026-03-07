from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..attachments import attachment_prompt_context
from ..capabilities import DEFAULT_CAPABILITY_REGISTRY
from ..models import NormalizedInboundEvent

if TYPE_CHECKING:
    from .core import Gateway


class ContextBuilder:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    def system_prompt(self, session_key: str, user_id: str, event: NormalizedInboundEvent) -> str:
        gw = self._gw
        capability_summary = "\n\n".join(
            item
            for item in (
                DEFAULT_CAPABILITY_REGISTRY.family_bundle(),
                gw.skills.capability_bundle(),
            )
            if item
        )
        workspace_context = gw.workspace.context_bundle(session_key, user_id)
        chunks = [
            "\n".join(
                [
                    f"You are {gw.agent_name}, a direct personal assistant for the user.",
                    f"Your name is {gw.agent_name}.",
                    f"If the user asks your name, answer with exactly {gw.agent_name} unless they ask for more detail.",
                    "Default to concise, high-signal replies suitable for Telegram chat.",
                    "Decide first whether the user wants conversation/thinking or a real-world action.",
                    "If conversation, drafting, brainstorming, rewriting, or explanation is enough, answer directly in chat.",
                    "Use external capabilities only when the task requires fetching, saving, creating, editing, sending, deleting, or otherwise touching the world outside the chat.",
                    "Do not let domain words alone trigger execution.",
                    "Use durable memory and recent session context when relevant, but never claim to remember something unless it appears in the provided memory context.",
                    "If the user asks for current or live information and you do not have fetched evidence in context, say you need a fetch/source instead of guessing.",
                    "Latest/current/news-style questions may be routed through a Codex CLI-backed web search helper before you answer.",
                    "If an action is pending approval, say it is pending; do not imply it already happened.",
                    "Treat the user respectfully and practically. Do not be servile or demeaning.",
                    "Rely on the capability summary, skill index, and playbooks to decide what exists; read detailed skills only when needed.",
                    "Do not enumerate or advertise every available capability unless the user asks.",
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
        if self.is_heartbeat(event):
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
