from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .identifiers import safe_component
from .memory import MemoryStore


DEFAULT_WORKSPACE_FILES = {
    "ASSISTANT.md": "\n".join(
        [
            "# Assistant",
            "",
            "- Talk like a calm, practical human by default.",
            "- Be concise unless the user asks for depth.",
            "- Do not narrate tools or internal state unless it helps the user.",
            "- Be adaptive during chat, but keep identity stable and clear.",
        ]
    )
    + "\n",
    "USER.md": "\n".join(
        [
            "# User",
            "",
            "- Add stable preferences, boundaries, and relationship notes here.",
        ]
    )
    + "\n",
    "OPERATIONS.md": "\n".join(
        [
            "# Operations",
            "",
            "- Skills are indexed by default and read on demand.",
            "- Prefer inspect/help/schema before unfamiliar command execution.",
            "- Ask for approval only for outward or destructive actions.",
        ]
    )
    + "\n",
    "NOW.md": "\n".join(
        [
            "# Now",
            "",
            "- Add active projects, short-term focus, and temporary assumptions here.",
        ]
    )
    + "\n",
    "SELF.md": "\n".join(
        [
            "# Self",
            "",
            "I am Synapse, an async Python agent runtime.",
            "",
            "## What I Am",
            "- A stateful agent runtime with explicit session state machines",
            "- I manage conversations as tracked runs: RECEIVED -> PLANNED -> EXECUTING -> COMPLETED",
            "- I gate risky actions behind human approval before executing",
            "- I store durable memory as markdown files (session/user/global)",
            "- I connect to Telegram and Google Workspace (Gmail, Calendar, Drive, Docs, Sheets)",
            "",
            "## How I Work",
            "- Gateway orchestrates: context build -> planning -> capability check -> execution -> response",
            "- Capability broker decides what's safe, risky, or needs approval",
            "- Plugin system: skills (capabilities), channels (adapters), hooks (lifecycle events)",
            "- 19 bundled skills ship out of the box",
            "",
            "## What I Can Do",
            "- Read/send Gmail, check calendar, search Drive, create Docs/Sheets",
            "- Remember things across sessions (durable markdown memory)",
            "- Search the web, run shell commands (with approval)",
            "- Schedule reminders, run proactive heartbeat checks",
            "- Propose and activate new integrations",
            "",
            "## What I Cannot Do (Yet)",
            "- Auto-apply code patches (disabled, proposal only)",
            "- Run commands in a real Docker sandbox (host execution only)",
            "- Connect to channels beyond Telegram",
            "- Self-author new plugins autonomously",
            "",
            "## My Values",
            "- Explicit over implicit: state machines, not hidden flows",
            "- Approval gates over blind autonomy",
            "- Operator visibility: everything is auditable",
            "- Markdown memory over opaque vector stores",
        ]
    )
    + "\n",
}


@dataclass(slots=True)
class WorkspaceSnapshot:
    files: list[dict[str, str]]
    playbooks: list[dict[str, str]]


class WorkspaceStore:
    def __init__(self, root: Path, memory: MemoryStore) -> None:
        self.root = root
        self.memory = memory
        self.assistant_path = root / "ASSISTANT.md"
        self.user_path = root / "USER.md"
        self.operations_path = root / "OPERATIONS.md"
        self.now_path = root / "NOW.md"
        self.heartbeat_path = root / "HEARTBEAT.md"
        self.self_path = root / "SELF.md"
        self.playbooks_dir = root / "playbooks"

    def initialize(self) -> None:
        self.playbooks_dir.mkdir(parents=True, exist_ok=True)
        for name, content in DEFAULT_WORKSPACE_FILES.items():
            path = self.root / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")

    def context_bundle(self, session_key: str, user_id: str, *, transcript_limit: int = 15) -> str:
        sections: list[str] = []
        for title, path in (
            ("Self", self.self_path),
            ("Assistant Profile", self.assistant_path),
            ("User Profile", self.user_path),
            ("Operations", self.operations_path),
            ("Current State", self.now_path),
        ):
            summary = self._summary(path, limit=1400)
            if summary:
                sections.append(f"## {title}\n{summary}")
        playbook_index = self.playbook_index_bundle()
        if playbook_index:
            sections.append("## Playbooks\n" + playbook_index)
        memory_summary = self._memory_summary(session_key, user_id, transcript_limit=transcript_limit)
        if memory_summary:
            sections.append(memory_summary)
        return "\n\n".join(sections)

    def playbook_index_bundle(self) -> str:
        entries = []
        for path in sorted(self.playbooks_dir.glob("*.md")):
            title = self._extract_title(path)
            summary = self._summary(path, limit=220)
            if summary:
                entries.append(f"- {title} | {path} | {summary.splitlines()[0]}")
            else:
                entries.append(f"- {title} | {path}")
        return "\n".join(entries)

    def select_playbooks(self, text: str, *, limit: int = 3) -> list[str]:
        lowered = text.lower()
        selected: list[tuple[int, str]] = []
        for path in self.playbooks_dir.glob("*.md"):
            haystack = f"{path.stem} {path.read_text(encoding='utf-8', errors='ignore')[:500]}".lower()
            score = sum(1 for token in lowered.split() if len(token) >= 4 and token in haystack)
            if score > 0:
                selected.append((score, path.stem))
        selected.sort(key=lambda item: (-item[0], item[1]))
        return [stem for _, stem in selected[:limit]]

    def read_playbooks(self, names: list[str]) -> str:
        chunks = []
        for name in names:
            path = self.playbooks_dir / f"{safe_component(name, max_length=80)}.md"
            if not path.exists():
                continue
            chunks.append(f"# Playbook: {self._extract_title(path)}\n# Path: {path}\n\n{path.read_text(encoding='utf-8').strip()}")
        return "\n\n".join(chunks)

    def promote_playbook(self, *, intent: str, skill_ids: list[str], commands: list[str], note: str) -> Path | None:
        if not intent or not commands:
            return None
        filename = safe_component(intent, max_length=80) or "playbook"
        path = self.playbooks_dir / f"{filename}.md"
        lines = []
        if skill_ids:
            lines.append(f"- skills: {', '.join(skill_ids)}")
        lines.append(f"- intent: {intent}")
        lines.append(f"- commands: {' | '.join(commands)}")
        if note:
            lines.append(f"- note: {note}")
        entry = " ; ".join(lines)
        if path.exists():
            existing = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if entry in existing:
                return path
            body = path.read_text(encoding="utf-8").rstrip() + "\n" + entry + "\n"
        else:
            title = intent.replace(".", " ").replace("_", " ").strip().title() or "Playbook"
            body = "\n".join([f"# {title}", "", entry]) + "\n"
        path.write_text(body, encoding="utf-8")
        return path

    def snapshot(self) -> dict[str, object]:
        return {
            "files": [self._file_snapshot(path) for path in self._workspace_files()],
            "playbooks": [self._file_snapshot(path) for path in sorted(self.playbooks_dir.glob("*.md"))],
        }

    def _workspace_files(self) -> list[Path]:
        paths = [self.self_path, self.assistant_path, self.user_path, self.operations_path, self.now_path, self.heartbeat_path]
        return [path for path in paths if path.exists()]

    def _file_snapshot(self, path: Path) -> dict[str, str]:
        return {"name": path.name, "path": str(path), "preview": self._summary(path, limit=240)}

    def _extract_title(self, path: Path) -> str:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("# "):
                return line.removeprefix("# ").strip() or path.stem
        return path.stem

    def _summary(self, path: Path, *, limit: int) -> str:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        return text[:limit]

    def _memory_summary(self, session_key: str, user_id: str, *, transcript_limit: int) -> str:
        sections: list[str] = []
        user_memory = self.memory.read_user_memory(user_id).strip()
        if user_memory:
            sections.append("### User Memory\n" + user_memory[:1000])
        session_notes = self.memory.read_session_notes(session_key).strip()
        if session_notes:
            sections.append("### Session Notes\n" + session_notes[:800])
        session_summary = self.memory.read_session_summary(session_key).strip()
        if session_summary:
            sections.append("### Session Summary\n" + session_summary[:800])
        current_task = self.memory.read_current_task(session_key)
        if current_task:
            rendered = json.dumps(current_task, ensure_ascii=True, indent=2)
            sections.append("### Current Task\n" + rendered[:1600])
        transcript_entries = self.memory.read_recent_transcript(session_key, limit=transcript_limit)
        if transcript_entries:
            transcript_lines = []
            for entry in transcript_entries:
                role = str(entry.get("role", "unknown"))
                content = str(entry.get("content", "")).strip()
                if content:
                    transcript_lines.append(f"{role}: {content}")
            if transcript_lines:
                sections.append("### Recent Transcript\n" + "\n".join(transcript_lines[:transcript_limit]))
        global_memory = self.memory.read_global_memory().strip()
        if global_memory:
            sections.append("### Durable Memory\n" + global_memory[:800])
        return "## Memory\n" + "\n\n".join(sections) if sections else ""
