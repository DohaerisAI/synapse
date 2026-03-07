import json

from synapse.memory import MemoryStore
from synapse.skills import SkillRegistry


def test_skill_registry_loads_skill_manifest_and_markdown(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "ops"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text(
        json.dumps({"id": "ops", "name": "Ops", "description": "ops skill"}),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("Keep responses terse.", encoding="utf-8")

    registry = SkillRegistry(tmp_path / "skills")
    skills = registry.load()

    assert "ops" in skills
    assert "ops skill" in registry.context_bundle()
    assert "Keep responses terse." not in registry.context_bundle()
    assert "Keep responses terse." in registry.read(["ops"])


def test_memory_store_structures_and_deduplicates_user_memory(tmp_path) -> None:
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()

    memory.append_user_memory("user-1", "User prefers to be called AD")
    memory.append_user_memory("user-1", "User prefers to be called AD.")
    memory.append_user_memory("user-1", "My timezone is UTC")

    text = memory.read_user_memory("user-1")

    assert text.count("User prefers to be called AD.") == 1
    assert "## Preferences" in text
    assert "## Facts" in text
    assert "My timezone is UTC." in text


def test_memory_store_uses_scoped_paths(tmp_path) -> None:
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()
    memory.append_transcript("telegram__chat__user", {"role": "user", "content": "hello"})
    memory.append_global_memory("world", name="../escape")

    transcript = (tmp_path / "memory" / "sessions" / "telegram__chat__user" / "transcript.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"content": "hello"' in transcript
    assert (tmp_path / "memory" / "global" / "escape.md").exists()


def test_memory_store_context_bundle_includes_user_session_and_transcript(tmp_path) -> None:
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()
    memory.append_user_memory("user-1", "User prefers to be called AD.")
    memory.append_notes("telegram__chat__user-1", "Short-term note.")
    memory.write_summary("telegram__chat__user-1", "# Summary\n\nLast topic: testing")
    memory.append_transcript("telegram__chat__user-1", {"role": "user", "content": "hello"})

    bundle = memory.context_bundle("telegram__chat__user-1", "user-1")

    assert "## User Memory" in bundle
    assert "User prefers to be called AD." in bundle
    assert "## Session Notes" in bundle
    assert "## Session Summary" in bundle
    assert "## Recent Transcript" in bundle
