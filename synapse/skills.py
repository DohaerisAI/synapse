from __future__ import annotations

import json
import re
from pathlib import Path

from .models import SkillDefinition


class SkillRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.skills: dict[str, SkillDefinition] = {}

    def load(self) -> dict[str, SkillDefinition]:
        self.skills = {}
        if not self.root.exists():
            return self.skills
        for skill_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
            manifest_path = skill_dir / "manifest.json"
            instruction_path = skill_dir / "SKILL.md"
            if not manifest_path.exists() or not instruction_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            skill_id = manifest.get("id", skill_dir.name)
            self.skills[skill_id] = SkillDefinition(
                skill_id=skill_id,
                name=manifest.get("name", skill_id),
                description=manifest.get("description", ""),
                instruction_markdown=instruction_path.read_text(encoding="utf-8").strip(),
                path=str(instruction_path),
                capabilities=list(manifest.get("capabilities", [])),
            )
        return self.skills

    def get(self, skill_id: str) -> SkillDefinition | None:
        return self.skills.get(skill_id)

    def read(self, skill_ids: list[str]) -> str:
        chunks = []
        for skill_id in skill_ids:
            skill = self.skills.get(skill_id)
            if skill is None:
                continue
            chunks.append(f"# Skill: {skill.name}\n# Path: {skill.path}\n\n{skill.instruction_markdown}")
        return "\n\n".join(chunks)

    def index_bundle(self, skill_ids: list[str] | None = None) -> str:
        if skill_ids is None:
            selected = list(self.skills.values())
        else:
            selected = [self.skills[skill_id] for skill_id in skill_ids if skill_id in self.skills]
        if not selected:
            return ""
        lines = ["Available skills:"]
        for skill in selected:
            capability_text = f" [{', '.join(skill.capabilities)}]" if skill.capabilities else ""
            lines.append(f"- {skill.skill_id}: {skill.description}{capability_text} | {skill.path}")
        return "\n".join(lines)

    def context_bundle(self, skill_ids: list[str] | None = None) -> str:
        return self.index_bundle(skill_ids)

    def capability_bundle(self) -> str:
        grouped: dict[str, list[str]] = {}
        for skill in self.skills.values():
            capabilities = skill.capabilities or ["general"]
            for capability in capabilities:
                grouped.setdefault(capability, []).append(skill.skill_id)
        if not grouped:
            return ""
        lines = ["Available capability families:"]
        for capability in sorted(grouped):
            skill_ids = ", ".join(sorted(grouped[capability]))
            lines.append(f"- {capability}: {skill_ids}")
        return "\n".join(lines)

    def check_readiness(self) -> dict[str, dict[str, object]]:
        report: dict[str, dict[str, object]] = {}
        for skill_id, skill in self.skills.items():
            entry = {"ready": True, "issues": []}
            if not skill.instruction_markdown.strip():
                entry["ready"] = False
                entry["issues"].append("empty instruction markdown")
            report[skill_id] = entry
        return report

    def select_candidates(self, text: str, *, capability: str | None = None, limit: int = 6) -> list[str]:
        lowered = text.lower()
        tokens = set(re.findall(r"[a-z0-9@._+-]+", lowered))
        scored: list[tuple[int, str]] = []
        explicit_gws = any(
            marker in lowered
            for marker in (
                "gws",
                "gmail",
                "calendar",
                "drive",
                "docs",
                "sheets",
                "workspace",
                "spreadsheet",
                "google doc",
                "google sheet",
                "mail",
                "email",
                "meeting",
            )
        )
        for skill_id, skill in self.skills.items():
            if capability and capability not in skill.capabilities:
                continue
            haystack = " ".join([skill.skill_id, skill.name, skill.description]).lower()
            score = 0
            for token in tokens:
                if len(token) < 3:
                    continue
                if token in haystack:
                    score += 2
            if capability and capability in skill.capabilities:
                score += 1
            if score > 0:
                scored.append((score, skill_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        ordered = [skill_id for _, skill_id in scored[:limit]]
        if capability == "gws" and explicit_gws and "gws-shared" in self.skills and "gws-shared" not in ordered:
            ordered.insert(0, "gws-shared")
        return ordered[:limit]
