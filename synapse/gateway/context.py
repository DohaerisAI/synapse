from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..attachments import attachment_prompt_context
from ..models import NormalizedInboundEvent

if TYPE_CHECKING:
    from .core import Gateway


_BASE_INSTRUCTIONS = [
    "Default to concise, high-signal replies suitable for Telegram chat.",
    "Be direct and useful. No fluff, no performative hedging, no motivational filler.",
    "Be resourceful. If repo state, diffs, workspace files, or current information matter, inspect or fetch them with tools instead of guessing.",
    "Never claim you checked, verified, searched, read, diffed, ran, or confirmed anything unless a tool call in this turn actually did it.",
    "Use durable memory and recent session context when relevant, but never claim to remember something unless it appears in the provided memory context.",
    "If the user asks for current or live information: use your tools (prefer swing_analyze/swing_scan, then shell_readonly/shell_exec) to fetch it. Only say you cannot if the tool call itself fails.",
    "With AD, keep the tone casually familiar and concise. Bro-level casual is fine; rambling is not.",
    "Treat the user respectfully and practically. Do not be servile or demeaning.",
    "Do not enumerate or advertise every available capability unless the user asks.",
]


class ContextBuilder:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    def _repo_root(self) -> Path:
        gw = self._gw
        root = getattr(getattr(gw, "config", None), "paths", None)
        if root is not None and getattr(root, "root", None):
            return Path(root.root)
        return Path(getattr(gw, "root", None) or Path.cwd())

    def _read_text_file(self, path: Path) -> str:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
        return ""

    def _prompt_bootstrap_chunks(self) -> list[str]:
        """OpenClaw-like bootstrap injection, but aligned to Synapse repo structure.

        Primary sources:
        - repo root: SELF.md, USER.md
        - var/prompts: TOOLS.md, HEARTBEAT.md (and optional overrides)
        """
        base = self._repo_root()
        chunks: list[str] = []

        prompts_dir = base / "var" / "prompts"

        # Prefer var/prompts/SELF.md + USER.md (canonical for Synapse),
        # fallback to repo-root legacy files.
        self_md = self._read_text_file(prompts_dir / "SELF.md") or self._read_text_file(base / "SELF.md")
        user_md = self._read_text_file(prompts_dir / "USER.md") or self._read_text_file(base / "USER.md")

        tools_md = self._read_text_file(prompts_dir / "TOOLS.md")
        hb_md = self._read_text_file(prompts_dir / "HEARTBEAT.md")

        if self_md:
            chunks.append("SELF:\n" + self_md)
        if user_md:
            chunks.append("USER:\n" + user_md)
        if tools_md:
            chunks.append("TOOLS:\n" + tools_md)
        if hb_md:
            chunks.append("HEARTBEAT POLICY:\n" + hb_md)
        return chunks

    def _tooling_summary(self, *, limit: int = 40) -> str:
        gw = self._gw
        reg = getattr(gw, "tool_registry", None)
        if reg is None:
            return ""
        tools = []
        try:
            tools = list(reg.all_tools())
        except Exception:
            return ""
        lines: list[str] = ["Available tools (name — approval — category):"]
        for tool in tools[:limit]:
            needs = getattr(tool, "needs_approval", False)
            needs_flag = "yes" if bool(needs) else "no"
            cat = getattr(tool, "category", "") or ""
            lines.append(f"- {tool.name} — approval:{needs_flag} — {cat}")
        if len(tools) > limit:
            lines.append(f"- … +{len(tools)-limit} more")
        return "\n".join(lines)

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
        bootstrap = self._prompt_bootstrap_chunks()
        tooling = self._tooling_summary()
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
        if tooling:
            chunks.append(tooling)
        if bootstrap:
            chunks.append("\n\n".join(bootstrap))
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
        bootstrap = self._prompt_bootstrap_chunks()
        tooling = self._tooling_summary()
        chunks = [
            "\n".join(
                self._identity_lines()
                + _BASE_INSTRUCTIONS
                + [
                    "Use the tools provided to accomplish tasks. Call tools directly — do not describe actions you would take.",
                    "If the task is purely conversational (drafting, brainstorming, explaining), answer directly in chat without calling tools.",
                    "When a task matches a skill, ALWAYS: (1) call load_skill to get instructions, (2) call shell_exec with the command shown in the skill. This applies to gws-* skills (CLI commands), scanner scripts (python3), and any skill with a command-line interface. Never skip the execution step or assume it will fail.",
                    "When the user asks what changed in the repo, to show code changes, or for a diff, ALWAYS inspect the repo first with shell_exec using `git status -sb` and `git diff --stat` with `cwd` set to the repo (or `git -C <repo>` if needed) before answering.",
                    "Use the read-only repo helpers (`repo_status`, `repo_diffstat`, `repo_diff`) when they reduce mistakes, but repo-change requests still require the git inspection step above.",
                    "Prefer `repo_open`/`repo_grep`/`repo_diff`/`repo_diffstat` for repo inspection when they fit the request.",
                    "Prefer `shell_readonly` over `shell_exec` for simple read-only shell checks (ls/pwd/whoami/git status/diff/rg/grep/cat/head/tail/sed -n) to avoid unnecessary approvals.",
                    "Do not use Zerodha/Kite trade tools unless the user explicitly asks for Kite/Zerodha data or order placement.",
                    "If the user asks for live/current prices/RSI/support-resistance or live analysis, call `swing_analyze` (or `swing_scan` for watchlist scans) before returning numeric values. Do not ask the user to repeat trigger phrases; proceed immediately.",
                    "Prefer `fs_read`/`fs_write`/`fs_edit` for file reads and direct file edits instead of raw shell file commands.",
                    "Prefer `patch_apply` over ad hoc shell patching when applying a unified diff to the working tree.",
                    "shell_exec is not an interactive shell. Prefer setting `cwd` and giving the exact command with direct args instead of relying on shell state, prompts, or cd chains.",
                    "NEVER claim a tool call failed unless you actually called it and received an error response. If you have not executed shell_exec yet, you cannot know whether it will succeed or fail.",
                    "Heartbeat is a periodic proactive review, not the same thing as a reminder timer. Use reminder features for time-based follow-ups.",
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
                "load_skill('swing-trader') → swing_scan(pattern='all', watchlist='nifty50', mode='trade_ready')."
            )
        if tooling:
            chunks.append(tooling)
        if bootstrap:
            chunks.append("\n\n".join(bootstrap))
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
