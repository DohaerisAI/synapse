from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import yaml

from .models import SkillDefinition

logger = logging.getLogger(__name__)

ALLOWED_SKILL_BUNDLE_TOP_LEVEL = frozenset(
    {
        "manifest.json",
        "SKILL.md",
        "requirements.txt",
        "scripts",
        "references",
        "assets",
        "watchlists",
    }
)
SAFE_SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SkillBundleError(ValueError):
    """Raised when a skill bundle is malformed or unsafe."""


class SkillRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.skills: dict[str, SkillDefinition] = {}
        self._skill_tools: dict[str, list[dict]] = {}
        self._lock = threading.RLock()

    def load(self) -> dict[str, SkillDefinition]:
        skills: dict[str, SkillDefinition] = {}
        skill_tools: dict[str, list[dict]] = {}
        if not self.root.exists():
            with self._lock:
                self.skills = skills
                self._skill_tools = skill_tools
                return dict(self.skills)
        for skill_dir in _iter_skill_dirs(self.root):
            manifest_path = skill_dir / "manifest.json"
            instruction_path = skill_dir / "SKILL.md"
            if not manifest_path.exists() or not instruction_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            metadata, instruction_markdown = _parse_skill_markdown(instruction_path.read_text(encoding="utf-8"))
            skill_id = manifest.get("id", skill_dir.name)
            skills[skill_id] = SkillDefinition(
                skill_id=skill_id,
                name=manifest.get("name", skill_id),
                description=manifest.get("description", ""),
                instruction_markdown=instruction_markdown,
                path=str(instruction_path),
                capabilities=list(manifest.get("capabilities", [])),
                metadata=metadata,
            )
            # Store optional tools from manifest for auto-registration
            tools = manifest.get("tools")
            if isinstance(tools, list) and tools:
                skill_tools[skill_id] = tools
        with self._lock:
            self.skills = skills
            self._skill_tools = skill_tools
            return dict(self.skills)

    def get_skill_tools(self, skill_id: str) -> list[dict]:
        """Return the tools array from a skill's manifest, if any."""
        with self._lock:
            return list(self._skill_tools.get(skill_id, []))

    def get(self, skill_id: str) -> SkillDefinition | None:
        with self._lock:
            return self.skills.get(skill_id)

    def find_by_path(self, path: Path) -> SkillDefinition | None:
        candidate = path.resolve(strict=False)
        with self._lock:
            skills = list(self.skills.values())
        for skill in skills:
            if not skill.path:
                continue
            skill_root = Path(skill.path).resolve(strict=False).parent
            try:
                candidate.relative_to(skill_root)
            except ValueError:
                continue
            return skill
        return None

    def read(self, skill_ids: list[str]) -> str:
        with self._lock:
            selected = [self.skills.get(skill_id) for skill_id in skill_ids]
        chunks = []
        for skill in selected:
            if skill is None:
                continue
            chunks.append(f"# Skill: {skill.name}\n# Path: {skill.path}\n\n{skill.instruction_markdown}")
        return "\n\n".join(chunks)

    def index_bundle(self, skill_ids: list[str] | None = None) -> str:
        with self._lock:
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
        with self._lock:
            skills = list(self.skills.values())
        for skill in skills:
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
        with self._lock:
            items = list(self.skills.items())
        for skill_id, skill in items:
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
        with self._lock:
            items = list(self.skills.items())
            has_shared_gws = "gws-shared" in self.skills
        for skill_id, skill in items:
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
        if capability == "gws" and explicit_gws and has_shared_gws and "gws-shared" not in ordered:
            ordered.insert(0, "gws-shared")
        return ordered[:limit]


class SkillHotReloader:
    DEFAULT_POLL_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        root: Path,
        *,
        reload_callback,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self.root = root
        self._reload_callback = reload_callback
        self._poll_interval_seconds = poll_interval_seconds or self.DEFAULT_POLL_INTERVAL_SECONDS
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_snapshot = self.snapshot()

    def start(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._stop_event.clear()
        self._last_snapshot = self.snapshot()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="skill-hot-reloader",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def snapshot(self) -> str:
        digest = hashlib.sha256()
        digest.update(str(self.root).encode("utf-8"))
        if not self.root.exists():
            digest.update(b":missing")
            return digest.hexdigest()
        for skill_dir in _iter_skill_dirs(self.root):
            digest.update(f"dir:{skill_dir.name}\n".encode("utf-8"))
            for rel_name in ("manifest.json", "SKILL.md"):
                target = skill_dir / rel_name
                if not target.exists():
                    digest.update(f"{rel_name}:missing\n".encode("utf-8"))
                    continue
                stat = target.stat()
                digest.update(
                    f"{rel_name}:{stat.st_mtime_ns}:{stat.st_size}\n".encode("utf-8")
                )
        return digest.hexdigest()

    def _watch_loop(self) -> None:
        while not self._stop_event.wait(self._poll_interval_seconds):
            try:
                snapshot = self.snapshot()
            except Exception:
                logger.warning("skill hot reload snapshot failed", exc_info=True)
                continue
            if snapshot == self._last_snapshot:
                continue
            try:
                self._reload_callback()
            except Exception:
                logger.warning("skill hot reload failed", exc_info=True)
                continue
            self._last_snapshot = snapshot


def _parse_skill_markdown(markdown: str) -> tuple[dict, str]:
    text = markdown.strip()
    if not markdown.startswith("---\n"):
        return {}, text
    lines = markdown.splitlines()
    try:
        closing_index = lines[1:].index("---") + 1
    except ValueError:
        return {}, text
    frontmatter = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1:]).strip()
    try:
        parsed = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(parsed, dict):
        return {}, text
    return _normalize_skill_metadata(parsed), body


def _normalize_skill_metadata(metadata: dict) -> dict:
    normalized = dict(metadata)
    for key in ("network", "mount_workspace"):
        if key not in normalized:
            continue
        value = normalized[key]
        if isinstance(value, bool):
            continue
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "1", "on"}:
                normalized[key] = True
                continue
            if lowered in {"false", "no", "0", "off"}:
                normalized[key] = False
                continue
        normalized.pop(key, None)
    return normalized


def install_skill_bundle(*, root: Path, skills_dir: Path, source_path: str | Path) -> dict[str, object]:
    source = Path(source_path)
    if not source.is_absolute():
        source = (root / source).resolve()
    if not source.exists():
        raise FileNotFoundError(f"skill source not found: {source}")

    install_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    state_root = root / "var" / "skills"
    staging_dir = state_root / "staging" / install_id
    backups_dir = state_root / "backups"
    registry_dir = state_root / "registry"
    staging_dir.mkdir(parents=True, exist_ok=False)
    backups_dir.mkdir(parents=True, exist_ok=True)
    registry_dir.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    destination: Path | None = None
    installed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        staged_bundle, source_info = _stage_skill_bundle(source, staging_dir)
        manifest = _load_and_validate_manifest(staged_bundle)
        skill_id = str(manifest["id"]).strip()
        checksum = _bundle_checksum(staged_bundle)
        destination = skills_dir / skill_id
        promote_dir = skills_dir / f".install-{install_id}-{skill_id}"
        existing_backup_dir = skills_dir / f".backup-{install_id}-{skill_id}"

        skills_dir.mkdir(parents=True, exist_ok=True)
        try:
            if promote_dir.exists():
                shutil.rmtree(promote_dir, ignore_errors=True)
            shutil.copytree(staged_bundle, promote_dir)
            if destination.exists():
                if existing_backup_dir.exists():
                    shutil.rmtree(existing_backup_dir, ignore_errors=True)
                os.replace(destination, existing_backup_dir)

            os.replace(promote_dir, destination)

            if existing_backup_dir.exists():
                backup_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup_path = backups_dir / f"{backup_stamp}-{skill_id}"
                shutil.move(str(existing_backup_dir), str(backup_path))

            registry_path = registry_dir / f"{skill_id}.json"
            record = {
                "skill_id": skill_id,
                "checksum": checksum,
                "installed_at": installed_at,
                "install_id": install_id,
                "source": source_info,
                "destination": str(destination),
                "backup_path": str(backup_path) if backup_path is not None else None,
            }
            registry_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        except Exception:
            if promote_dir.exists():
                shutil.rmtree(promote_dir, ignore_errors=True)
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            if existing_backup_dir.exists():
                os.replace(existing_backup_dir, destination)
            elif backup_path is not None and backup_path.exists():
                shutil.move(str(backup_path), str(destination))
            raise

        return {
            "ok": True,
            "install_id": install_id,
            "skill_id": skill_id,
            "destination": str(destination),
            "backup_path": str(backup_path) if backup_path is not None else None,
            "registry_path": str(registry_dir / f"{skill_id}.json"),
            "staging_path": str(staging_dir),
            "checksum": checksum,
            "installed_at": installed_at,
            "source": source_info,
        }
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _stage_skill_bundle(source: Path, staging_dir: Path) -> tuple[Path, dict[str, str]]:
    bundle_dir = staging_dir / "bundle"
    if source.is_dir():
        bundle_root = _locate_bundle_root(source)
        _validate_bundle_tree(bundle_root)
        shutil.copytree(bundle_root, bundle_dir)
        _load_and_validate_manifest(bundle_dir)
        return bundle_dir, {"type": "directory", "path": str(source)}

    if source.is_file() and source.suffix.lower() == ".zip":
        extract_root = staging_dir / "source"
        extract_root.mkdir(parents=True, exist_ok=True)
        _extract_zip_bundle(source, extract_root)
        bundle_root = _locate_bundle_root(extract_root)
        _validate_bundle_tree(bundle_root)
        shutil.copytree(bundle_root, bundle_dir)
        _load_and_validate_manifest(bundle_dir)
        return bundle_dir, {"type": "zip", "path": str(source)}

    raise SkillBundleError(f"unsupported skill source: {source}")


def _locate_bundle_root(root: Path) -> Path:
    if _looks_like_bundle_root(root):
        return root

    children = sorted(root.iterdir(), key=lambda item: item.name)
    if len(children) != 1 or not children[0].is_dir():
        raise SkillBundleError("skill bundle must contain manifest.json and SKILL.md at the top level")
    if not _looks_like_bundle_root(children[0]):
        raise SkillBundleError("skill bundle must contain manifest.json and SKILL.md at the top level")
    return children[0]


def _looks_like_bundle_root(root: Path) -> bool:
    return (root / "manifest.json").is_file() and (root / "SKILL.md").is_file()


def _load_and_validate_manifest(bundle_root: Path) -> dict:
    manifest_path = bundle_root / "manifest.json"
    instruction_path = bundle_root / "SKILL.md"
    if not manifest_path.is_file() or not instruction_path.is_file():
        raise SkillBundleError("skill bundle requires manifest.json and SKILL.md")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SkillBundleError(f"invalid manifest.json: {error.msg}") from error
    if not isinstance(manifest, dict):
        raise SkillBundleError("manifest.json must contain a JSON object")
    skill_id = str(manifest.get("id", "")).strip()
    if not skill_id:
        raise SkillBundleError("manifest.json must define a non-empty id")
    if not SAFE_SKILL_ID_PATTERN.fullmatch(skill_id):
        raise SkillBundleError(f"unsafe skill id: {skill_id}")
    return manifest


def _validate_bundle_tree(bundle_root: Path) -> None:
    entries = {entry.name: entry for entry in bundle_root.iterdir()}
    missing = [name for name in ("manifest.json", "SKILL.md") if name not in entries]
    if missing:
        raise SkillBundleError(f"missing required files: {', '.join(missing)}")

    unexpected = sorted(name for name in entries if name not in ALLOWED_SKILL_BUNDLE_TOP_LEVEL)
    if unexpected:
        raise SkillBundleError(f"unexpected top-level bundle entries: {', '.join(unexpected)}")

    for name, entry in entries.items():
        if entry.is_symlink():
            raise SkillBundleError(f"symlinks are not allowed in skill bundles: {name}")
        if name in {"manifest.json", "SKILL.md", "requirements.txt"} and not entry.is_file():
            raise SkillBundleError(f"{name} must be a file")
        if name in {"scripts", "references", "assets", "watchlists"} and not entry.is_dir():
            raise SkillBundleError(f"{name} must be a directory")

    for path in sorted(bundle_root.rglob("*")):
        relative = path.relative_to(bundle_root)
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise SkillBundleError(f"unsafe bundle path: {relative.as_posix()}")
        if relative.parts[0] not in ALLOWED_SKILL_BUNDLE_TOP_LEVEL:
            raise SkillBundleError(f"unexpected bundle path: {relative.as_posix()}")
        if path.is_symlink():
            raise SkillBundleError(f"symlinks are not allowed in skill bundles: {relative.as_posix()}")
        if path.is_dir() or path.is_file():
            continue
        raise SkillBundleError(f"unsupported bundle entry: {relative.as_posix()}")

    _load_and_validate_manifest(bundle_root)


def _extract_zip_bundle(source: Path, extract_root: Path) -> None:
    with ZipFile(source) as archive:
        for member in archive.infolist():
            relative = _safe_zip_relative_path(member.filename)
            if relative is None:
                continue
            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise SkillBundleError(f"zip bundle contains a symlink: {member.filename}")
            target = extract_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            with archive.open(member, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _safe_zip_relative_path(name: str) -> Path | None:
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", name):
        raise SkillBundleError(f"zip bundle contains an absolute path: {name}")
    normalized = name.replace("\\", "/").strip("/")
    if not normalized:
        return None
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SkillBundleError(f"zip bundle contains an unsafe path: {name}")
    return Path(*parts)


def _bundle_checksum(bundle_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(bundle_root.rglob("*")):
        relative = path.relative_to(bundle_root).as_posix()
        if path.is_dir():
            digest.update(f"dir:{relative}\n".encode("utf-8"))
            continue
        digest.update(f"file:{relative}\n".encode("utf-8"))
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    return digest.hexdigest()


def _iter_skill_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
