from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .models import IntegrationRecord, IntegrationStatus, utc_now


KNOWN_INTEGRATIONS: dict[str, dict[str, Any]] = {
    "google-calendar": {
        "kind": "service",
        "title": "Google Calendar",
        "summary": "Calendar scheduling and event lookup integration scaffold.",
        "required_env": ["GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET", "GOOGLE_CALENDAR_REFRESH_TOKEN"],
    },
    "gmail": {
        "kind": "service",
        "title": "Gmail",
        "summary": "Gmail inbox and message workflow integration scaffold.",
        "required_env": ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"],
    },
    "google-drive": {
        "kind": "service",
        "title": "Google Drive",
        "summary": "Google Drive file lookup and retrieval integration scaffold.",
        "required_env": ["GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET", "GOOGLE_DRIVE_REFRESH_TOKEN"],
    },
    "github": {
        "kind": "service",
        "title": "GitHub",
        "summary": "GitHub repository, issues, and pull request integration scaffold.",
        "required_env": ["GITHUB_TOKEN"],
    },
    "notion": {
        "kind": "service",
        "title": "Notion",
        "summary": "Notion notes and database integration scaffold.",
        "required_env": ["NOTION_TOKEN"],
    },
    "slack": {
        "kind": "channel",
        "title": "Slack",
        "summary": "Slack channel adapter integration scaffold.",
        "required_env": ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"],
    },
    "discord": {
        "kind": "channel",
        "title": "Discord",
        "summary": "Discord channel adapter integration scaffold.",
        "required_env": ["DISCORD_BOT_TOKEN"],
    },
    "signal": {
        "kind": "channel",
        "title": "Signal",
        "summary": "Signal channel adapter integration scaffold.",
        "required_env": ["SIGNAL_ACCOUNT_ID"],
    },
}


class IntegrationRegistry:
    def __init__(self, root: Path, *, skills_dir: Path, boot_path: Path, env: dict[str, str]) -> None:
        self.root = root
        self.skills_dir = skills_dir
        self.boot_path = boot_path
        self.env = env

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def list_integrations(self) -> list[IntegrationRecord]:
        self.initialize()
        records: list[IntegrationRecord] = []
        for manifest in sorted(self.root.glob("*/manifest.json")):
            record = self._load_manifest(manifest)
            if record is not None:
                records.append(record)
        return records

    def get(self, integration_id: str) -> IntegrationRecord | None:
        manifest = self._manifest_path(integration_id)
        if not manifest.exists():
            return None
        return self._load_manifest(manifest)

    def propose(self, request: str) -> IntegrationRecord:
        spec = self._infer_spec(request)
        existing = self.get(spec["integration_id"])
        if existing is not None:
            return existing
        now = utc_now().isoformat()
        record = IntegrationRecord(
            integration_id=spec["integration_id"],
            kind=spec["kind"],
            status=IntegrationStatus.PROPOSED,
            title=spec["title"],
            summary=spec["summary"],
            required_env=spec["required_env"],
            bootstrap_steps=[f"activate {spec['integration_id']}"],
            files=[],
            test_spec="validate manifest, staged skill, and BOOT activation line",
            created_at=now,
            updated_at=now,
        )
        self._write_manifest(record)
        return record

    def scaffold(self, integration_id: str) -> IntegrationRecord:
        record = self._require(integration_id)
        integration_dir = self.root / integration_id
        integration_dir.mkdir(parents=True, exist_ok=True)
        skill_dir = integration_dir / "skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        readme_path = integration_dir / "README.md"
        boot_path = integration_dir / "BOOT.md"
        skill_manifest = skill_dir / "manifest.json"
        skill_markdown = skill_dir / "SKILL.md"
        readme_path.write_text(self._integration_readme(record), encoding="utf-8")
        boot_path.write_text(f"activate {integration_id}\n", encoding="utf-8")
        skill_manifest.write_text(
            json.dumps(
                {
                    "id": integration_id,
                    "name": record.title,
                    "description": record.summary,
                    "capabilities": [f"integration:{record.kind}", f"integration:{integration_id}"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        skill_markdown.write_text(self._skill_markdown(record), encoding="utf-8")
        return self._update_record(
            record,
            status=IntegrationStatus.SCAFFOLDED,
            files=[
                str(readme_path.relative_to(self.root.parent)),
                str(boot_path.relative_to(self.root.parent)),
                str(skill_manifest.relative_to(self.root.parent)),
                str(skill_markdown.relative_to(self.root.parent)),
            ],
            last_error=None,
        )

    def test(self, integration_id: str) -> IntegrationRecord:
        record = self._require(integration_id)
        integration_dir = self.root / integration_id
        expected = [
            integration_dir / "README.md",
            integration_dir / "BOOT.md",
            integration_dir / "skill" / "manifest.json",
            integration_dir / "skill" / "SKILL.md",
        ]
        missing = [str(path.relative_to(self.root.parent)) for path in expected if not path.exists()]
        if missing:
            return self._update_record(
                record,
                status=IntegrationStatus.FAILED,
                last_error="missing scaffold files: " + ", ".join(missing),
            )
        missing_env = [key for key in record.required_env if not self.env.get(key, "").strip()]
        note = None if not missing_env else "missing env: " + ", ".join(missing_env)
        return self._update_record(record, status=IntegrationStatus.TESTED, last_error=note)

    def apply(self, integration_id: str) -> IntegrationRecord:
        record = self._require(integration_id)
        self._sync_skill(record.integration_id)
        self._ensure_boot_line(record.integration_id)
        missing_env = [key for key in record.required_env if not self.env.get(key, "").strip()]
        status = IntegrationStatus.ACTIVE if not missing_env else IntegrationStatus.APPROVED
        note = None if not missing_env else "missing env: " + ", ".join(missing_env)
        return self._update_record(record, status=status, last_error=note)

    def activate_existing(self) -> list[IntegrationRecord]:
        activated: list[IntegrationRecord] = []
        activation_targets = self._boot_activation_targets()
        for record in self.list_integrations():
            if activation_targets is not None and record.integration_id not in activation_targets:
                continue
            if record.status not in {IntegrationStatus.APPROVED, IntegrationStatus.ACTIVE}:
                continue
            self._sync_skill(record.integration_id)
            missing_env = [key for key in record.required_env if not self.env.get(key, "").strip()]
            status = IntegrationStatus.ACTIVE if not missing_env else IntegrationStatus.APPROVED
            note = None if not missing_env else "missing env: " + ", ".join(missing_env)
            activated.append(self._update_record(record, status=status, last_error=note))
        return activated

    def boot_tasks(self) -> list[str]:
        if not self.boot_path.exists():
            return []
        return [line.strip() for line in self.boot_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _boot_activation_targets(self) -> set[str] | None:
        tasks = self.boot_tasks()
        if not tasks:
            return None
        targets: set[str] = set()
        for task in tasks:
            if task.lower().startswith("activate "):
                target = task.split(" ", 1)[1].strip()
                if target:
                    targets.add(target)
        return targets or None

    def _sync_skill(self, integration_id: str) -> None:
        source_dir = self.root / integration_id / "skill"
        if not source_dir.exists():
            raise RuntimeError(f"integration {integration_id} has no staged skill")
        destination = self.skills_dir / integration_id
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source_dir, destination)

    def _ensure_boot_line(self, integration_id: str) -> None:
        lines = self.boot_tasks()
        entry = f"activate {integration_id}"
        if entry in lines:
            return
        lines.append(entry)
        content = "\n".join(lines).strip()
        self.boot_path.write_text((content + "\n") if content else "", encoding="utf-8")

    def _infer_spec(self, request: str) -> dict[str, Any]:
        requested = request.strip().lower()
        for prefix in ("add integration ", "install integration ", "install ", "enable integration "):
            if requested.startswith(prefix):
                requested = requested[len(prefix) :].strip()
                break
        integration_id = self._slugify(requested)
        known = KNOWN_INTEGRATIONS.get(integration_id)
        if known is not None:
            return {"integration_id": integration_id, **known}
        kind = "channel" if requested in {"slack", "discord", "signal", "matrix"} else "service"
        title = requested.replace("-", " ").title()
        env_prefix = integration_id.upper().replace("-", "_")
        required_env = [f"{env_prefix}_API_KEY"] if kind == "service" else [f"{env_prefix}_TOKEN"]
        summary = f"{title} {kind} integration scaffold."
        return {
            "integration_id": integration_id,
            "kind": kind,
            "title": title,
            "summary": summary,
            "required_env": required_env,
        }

    def _integration_readme(self, record: IntegrationRecord) -> str:
        required = "\n".join(f"- `{key}`" for key in record.required_env) or "- none"
        return "\n".join(
            [
                f"# {record.title}",
                "",
                record.summary,
                "",
                "## Status",
                "",
                f"- Kind: `{record.kind}`",
                f"- Registry status: `{record.status.value}`",
                "",
                "## Required env",
                "",
                required,
                "",
                "## Bootstrap",
                "",
                "\n".join(f"- `{step}`" for step in record.bootstrap_steps),
            ]
        ) + "\n"

    def _skill_markdown(self, record: IntegrationRecord) -> str:
        required = ", ".join(record.required_env) or "no extra env"
        return "\n".join(
            [
                f"# {record.title}",
                "",
                f"You represent the staged {record.title} integration.",
                f"Kind: {record.kind}.",
                f"Summary: {record.summary}",
                f"Required env for activation: {required}.",
                "If the integration is not active yet, explain what config is still missing instead of pretending the external service is connected.",
            ]
        ) + "\n"

    def _manifest_path(self, integration_id: str) -> Path:
        return self.root / integration_id / "manifest.json"

    def _require(self, integration_id: str) -> IntegrationRecord:
        record = self.get(integration_id)
        if record is None:
            raise RuntimeError(f"unknown integration: {integration_id}")
        return record

    def _write_manifest(self, record: IntegrationRecord) -> None:
        manifest_path = self._manifest_path(record.integration_id)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "integration_id": record.integration_id,
                    "kind": record.kind,
                    "status": record.status.value,
                    "title": record.title,
                    "summary": record.summary,
                    "required_env": record.required_env,
                    "bootstrap_steps": record.bootstrap_steps,
                    "files": record.files,
                    "test_spec": record.test_spec,
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                    "last_error": record.last_error,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _load_manifest(self, path: Path) -> IntegrationRecord | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return IntegrationRecord(
                integration_id=payload["integration_id"],
                kind=payload["kind"],
                status=IntegrationStatus(payload["status"]),
                title=payload["title"],
                summary=payload["summary"],
                required_env=list(payload.get("required_env", [])),
                bootstrap_steps=list(payload.get("bootstrap_steps", [])),
                files=list(payload.get("files", [])),
                test_spec=str(payload.get("test_spec", "")),
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
                last_error=payload.get("last_error"),
            )
        except Exception:
            return None

    def _update_record(
        self,
        record: IntegrationRecord,
        *,
        status: IntegrationStatus,
        files: list[str] | None = None,
        last_error: str | None,
    ) -> IntegrationRecord:
        updated = IntegrationRecord(
            integration_id=record.integration_id,
            kind=record.kind,
            status=status,
            title=record.title,
            summary=record.summary,
            required_env=list(record.required_env),
            bootstrap_steps=list(record.bootstrap_steps),
            files=list(record.files if files is None else files),
            test_spec=record.test_spec,
            created_at=record.created_at,
            updated_at=utc_now().isoformat(),
            last_error=last_error,
        )
        self._write_manifest(updated)
        return updated

    def _slugify(self, value: str) -> str:
        collapsed = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        return collapsed or "integration"
