from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .identifiers import safe_component

if TYPE_CHECKING:
    from .providers import ModelRouter


@dataclass(slots=True)
class MemorySearchResult:
    source: str
    line: int
    content: str
    score: float


@dataclass(slots=True)
class SessionMemoryPaths:
    session_dir: Path
    summary_path: Path
    notes_path: Path
    transcript_path: Path
    current_task_path: Path


USER_MEMORY_SECTIONS = ("Preferences", "Facts", "Tasks")
GLOBAL_MEMORY_SECTIONS = ("Notes",)


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.global_dir = root / "global"
        self.system_dir = root / "system"
        self.users_dir = root / "users"
        self.sessions_dir = root / "sessions"

    def initialize(self) -> None:
        self.global_dir.mkdir(parents=True, exist_ok=True)
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self.users_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def session_paths(self, session_key: str) -> SessionMemoryPaths:
        safe_session_key = safe_component(session_key, max_length=100)
        session_dir = self.sessions_dir / safe_session_key
        session_dir.mkdir(parents=True, exist_ok=True)
        return SessionMemoryPaths(
            session_dir=session_dir,
            summary_path=session_dir / "summary.md",
            notes_path=session_dir / "notes.md",
            transcript_path=session_dir / "transcript.jsonl",
            current_task_path=session_dir / "current_task.json",
        )

    def user_memory_path(self, user_id: str) -> Path:
        return self.users_dir / f"{safe_component(user_id, max_length=64)}.md"

    def global_memory_path(self, name: str = "general") -> Path:
        return self.global_dir / f"{safe_component(name, max_length=64)}.md"

    def skill_ops_memory_path(self) -> Path:
        return self.system_dir / "skill_ops.md"

    def append_transcript(self, session_key: str, entry: dict[str, object]) -> None:
        paths = self.session_paths(session_key)
        with paths.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=True) + "\n")

    def write_summary(self, session_key: str, content: str) -> None:
        paths = self.session_paths(session_key)
        paths.summary_path.write_text(content.rstrip() + "\n", encoding="utf-8")

    def write_current_task(self, session_key: str, payload: dict[str, object]) -> None:
        paths = self.session_paths(session_key)
        paths.current_task_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    def read_current_task(self, session_key: str) -> dict[str, object] | None:
        path = self.session_paths(session_key).current_task_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def clear_current_task(self, session_key: str) -> None:
        self.session_paths(session_key).current_task_path.unlink(missing_ok=True)

    def append_notes(self, session_key: str, content: str) -> None:
        paths = self.session_paths(session_key)
        self._append_unique_line(paths.notes_path, content)

    def append_user_memory(self, user_id: str, content: str) -> None:
        path = self.user_memory_path(user_id)
        section, item = self._classify_user_memory(content)
        self._upsert_markdown_item(path, "User Memory", USER_MEMORY_SECTIONS, section, item)

    def append_global_memory(self, content: str, *, name: str = "general") -> None:
        path = self.global_memory_path(name)
        item = self._normalize_memory_item(content)
        self._upsert_markdown_item(path, "Global Memory", GLOBAL_MEMORY_SECTIONS, "Notes", item)

    def append_skill_operation(self, *, skill_ids: list[str], intent: str, commands: list[str], note: str) -> None:
        path = self.skill_ops_memory_path()
        lines = []
        if skill_ids:
            lines.append(f"- skills: {', '.join(skill_ids)}")
        if intent:
            lines.append(f"- intent: {intent}")
        if commands:
            lines.append(f"- commands: {' | '.join(commands)}")
        if note:
            lines.append(f"- note: {note}")
        if not lines:
            return
        self._append_unique_line(path, " ; ".join(lines))

    def read_skill_operations(self) -> str:
        return self._read_text(self.skill_ops_memory_path())

    def delete_user_memory(self, user_id: str, content: str) -> bool:
        return self._delete_markdown_item(self.user_memory_path(user_id), USER_MEMORY_SECTIONS, content)

    def delete_global_memory(self, content: str, *, name: str = "general") -> bool:
        return self._delete_markdown_item(self.global_memory_path(name), GLOBAL_MEMORY_SECTIONS, content)

    def delete_session_notes(self, session_key: str, content: str) -> bool:
        path = self.session_paths(session_key).notes_path
        if not path.exists():
            return False
        target = content.strip().lower()
        if target in {"all", "*", "everything"}:
            path.unlink(missing_ok=True)
            return True
        lines = path.read_text(encoding="utf-8").splitlines()
        remaining = [line for line in lines if line.strip().lower() != target]
        if len(remaining) == len(lines):
            return False
        rendered = "\n".join(line for line in remaining if line.strip()).rstrip()
        if rendered:
            path.write_text(rendered + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        return True

    def read_session_summary(self, session_key: str) -> str:
        return self._read_text(self.session_paths(session_key).summary_path)

    def read_session_notes(self, session_key: str) -> str:
        return self._read_text(self.session_paths(session_key).notes_path)

    def read_user_memory(self, user_id: str) -> str:
        return self._read_text(self.user_memory_path(user_id))

    def read_global_memory(self, name: str = "general") -> str:
        return self._read_text(self.global_memory_path(name))

    def read_recent_transcript(self, session_key: str, *, limit: int = 15) -> list[dict[str, object]]:
        path = self.session_paths(session_key).transcript_path
        if not path.exists():
            return []
        entries: list[dict[str, object]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
        return entries[-limit:]

    def context_bundle(self, session_key: str, user_id: str, *, transcript_limit: int = 15) -> str:
        sections: list[str] = []
        user_memory = self.read_user_memory(user_id).strip()
        if user_memory:
            sections.append("## User Memory\n" + user_memory)
        session_notes = self.read_session_notes(session_key).strip()
        if session_notes:
            sections.append("## Session Notes\n" + session_notes)
        session_summary = self.read_session_summary(session_key).strip()
        if session_summary:
            sections.append("## Session Summary\n" + session_summary)
        current_task = self.read_current_task(session_key)
        if current_task:
            sections.append("## Current Task\n" + json.dumps(current_task, ensure_ascii=True, indent=2))
        transcript_entries = self.read_recent_transcript(session_key, limit=transcript_limit)
        if transcript_entries:
            transcript_lines = []
            for entry in transcript_entries:
                role = str(entry.get("role", "unknown"))
                content = str(entry.get("content", "")).strip()
                if content:
                    transcript_lines.append(f"{role}: {content}")
            if transcript_lines:
                sections.append("## Recent Transcript\n" + "\n".join(transcript_lines))
        global_memory = self.read_global_memory().strip()
        if global_memory:
            sections.append("## Global Memory\n" + global_memory)
        skill_ops = self.read_skill_operations().strip()
        if skill_ops:
            sections.append("## Skill Operations\n" + skill_ops)
        return "\n\n".join(sections)

    def token_estimate(self, session_key: str) -> int:
        path = self.session_paths(session_key).transcript_path
        if not path.exists():
            return 0
        text = path.read_text(encoding="utf-8")
        return len(text) // 4

    def transcript_entry_count(self, session_key: str) -> int:
        path = self.session_paths(session_key).transcript_path
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

    async def compact_transcript(
        self,
        session_key: str,
        model_router: ModelRouter,
        *,
        keep_recent: int = 6,
        max_entries: int = 20,
    ) -> bool:
        path = self.session_paths(session_key).transcript_path
        if not path.exists():
            return False
        raw_lines = path.read_text(encoding="utf-8").splitlines()
        entries: list[dict[str, object]] = []
        for raw in raw_lines:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    entries.append(parsed)
            except json.JSONDecodeError:
                continue
        if len(entries) <= max_entries:
            return False
        old_entries = entries[:-keep_recent]
        recent_entries = entries[-keep_recent:]
        old_text = "\n".join(
            f"{entry.get('role', 'unknown')}: {entry.get('content', '')}"
            for entry in old_entries
        )
        prompt = (
            "Summarize the following conversation transcript into a concise paragraph "
            "preserving key facts, decisions, and context:\n\n" + old_text
        )
        try:
            summary = await model_router.generate(
                system="You are a transcript summarizer. Return only the summary.",
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            return False
        self.write_summary(session_key, summary.strip())
        with path.open("w", encoding="utf-8") as handle:
            for entry in recent_entries:
                handle.write(json.dumps(entry, ensure_ascii=True) + "\n")
        return True

    def search(self, query: str, *, scope: str = "all") -> list[MemorySearchResult]:
        results: list[MemorySearchResult] = []
        tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        if not tokens:
            return results
        search_paths: list[tuple[str, Path]] = []
        if scope in {"all", "global"}:
            if self.global_dir.exists():
                search_paths.extend(("global", p) for p in self.global_dir.glob("*.md"))
        if scope in {"all", "system"}:
            if self.system_dir.exists():
                search_paths.extend(("system", p) for p in self.system_dir.glob("*.md"))
        if scope in {"all", "user"}:
            if self.users_dir.exists():
                search_paths.extend(("user", p) for p in self.users_dir.glob("*.md"))
        for source_label, file_path in search_paths:
            if not file_path.exists():
                continue
            for line_num, raw_line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
                line_lower = raw_line.lower()
                score = sum(1 for token in tokens if token in line_lower)
                if score > 0:
                    results.append(MemorySearchResult(
                        source=f"{source_label}:{file_path.name}",
                        line=line_num,
                        content=raw_line.strip(),
                        score=score / len(tokens),
                    ))
        results.sort(key=lambda r: -r.score)
        return results

    def snapshot(self) -> dict[str, object]:
        return {
            "global_files": self._file_summaries(self.global_dir.glob("*.md")),
            "system_files": self._file_summaries(self.system_dir.glob("*.md")),
            "user_files": self._file_summaries(self.users_dir.glob("*.md")),
            "session_files": self._session_summaries(),
        }

    def _session_summaries(self) -> list[dict[str, object]]:
        if not self.sessions_dir.exists():
            return []
        sessions = []
        for session_dir in sorted(path for path in self.sessions_dir.iterdir() if path.is_dir()):
            summary_path = session_dir / "summary.md"
            notes_path = session_dir / "notes.md"
            transcript_path = session_dir / "transcript.jsonl"
            current_task_path = session_dir / "current_task.json"
            sessions.append(
                {
                    "session_key": session_dir.name,
                    "summary_exists": summary_path.exists(),
                    "notes_exists": notes_path.exists(),
                    "transcript_exists": transcript_path.exists(),
                    "current_task_exists": current_task_path.exists(),
                    "summary_preview": self._preview(summary_path),
                    "notes_preview": self._preview(notes_path),
                    "current_task_preview": self._preview(current_task_path),
                }
            )
        return sessions

    def _file_summaries(self, paths: object) -> list[dict[str, object]]:
        summaries = []
        for path in sorted(paths):
            summaries.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "preview": self._preview(path),
                }
            )
        return summaries

    def _preview(self, path: Path, *, limit: int = 240) -> str | None:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        return text[:limit]

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _append_unique_line(self, path: Path, content: str) -> None:
        line = content.strip()
        if not line:
            return
        existing = []
        if path.exists():
            existing = [item.strip() for item in path.read_text(encoding="utf-8").splitlines()]
            if line in existing:
                return
        with path.open("a", encoding="utf-8") as handle:
            if existing and existing[-1]:
                handle.write("\n")
            handle.write(line.rstrip() + "\n")

    def _classify_user_memory(self, content: str) -> tuple[str, str]:
        item = self._normalize_memory_item(content)
        lowered = item.lower()
        if any(marker in lowered for marker in ("prefers", "likes", "called", "timezone", "works best", "usually")):
            return "Preferences", item
        if any(marker in lowered for marker in ("todo", "task", "follow up", "needs", "remind")):
            return "Tasks", item
        return "Facts", item

    def _normalize_memory_item(self, content: str) -> str:
        text = re.sub(r"\s+", " ", content.strip()).strip("- ").strip()
        return text.rstrip(".") + "." if text else ""

    def _upsert_markdown_item(
        self,
        path: Path,
        title: str,
        sections: tuple[str, ...],
        target_section: str,
        item: str,
    ) -> None:
        if not item:
            return
        if path.exists():
            section_map = self._parse_markdown_sections(path.read_text(encoding="utf-8"), sections)
        else:
            section_map = {section: [] for section in sections}
        normalized_existing = {existing.lower() for existing in section_map[target_section]}
        if item.lower() not in normalized_existing:
            section_map[target_section].append(item)
        path.write_text(self._render_markdown_sections(title, sections, section_map), encoding="utf-8")

    def _delete_markdown_item(self, path: Path, sections: tuple[str, ...], content: str) -> bool:
        if not path.exists():
            return False
        target = self._normalize_memory_item(content).lower()
        if target in {"all.", "*.", "everything."}:
            path.unlink(missing_ok=True)
            return True
        raw = path.read_text(encoding="utf-8")
        title = self._extract_markdown_title(raw)
        section_map = self._parse_markdown_sections(raw, sections)
        removed = False
        for section in sections:
            items = section_map.get(section, [])
            filtered = [item for item in items if item.lower() != target]
            if len(filtered) != len(items):
                removed = True
            section_map[section] = filtered
        if not removed:
            return False
        if all(not items for items in section_map.values()):
            path.unlink(missing_ok=True)
            return True
        path.write_text(self._render_markdown_sections(title, sections, section_map), encoding="utf-8")
        return True

    def _parse_markdown_sections(self, text: str, sections: tuple[str, ...]) -> dict[str, list[str]]:
        parsed = {section: [] for section in sections}
        current: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                heading = line.removeprefix("## ").strip()
                current = heading if heading in parsed else None
                continue
            if current and line.startswith("- "):
                parsed[current].append(line.removeprefix("- ").strip())
        return parsed

    def _render_markdown_sections(
        self,
        title: str,
        sections: tuple[str, ...],
        section_map: dict[str, list[str]],
    ) -> str:
        blocks = [f"# {title}"]
        for section in sections:
            items = section_map.get(section, [])
            blocks.extend(["", f"## {section}"])
            if items:
                blocks.extend([f"- {item}" for item in items])
        return "\n".join(blocks).rstrip() + "\n"

    def _extract_markdown_title(self, text: str) -> str:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("# "):
                return line.removeprefix("# ").strip() or "Memory"
        return "Memory"
