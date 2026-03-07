from synapse.memory import MemoryStore
from synapse.workspace import WorkspaceStore


def test_workspace_initialize_creates_default_files(tmp_path) -> None:
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()
    workspace = WorkspaceStore(tmp_path, memory)

    workspace.initialize()

    for name in ["ASSISTANT.md", "USER.md", "OPERATIONS.md", "NOW.md"]:
        assert (tmp_path / name).exists()
    assert (tmp_path / "playbooks").exists()


def test_workspace_context_bundle_uses_layered_files_and_memory(tmp_path) -> None:
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()
    workspace = WorkspaceStore(tmp_path, memory)
    workspace.initialize()

    (tmp_path / "ASSISTANT.md").write_text("# Assistant\n\nBe calm.\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("# User\n\nCall the user AD.\n", encoding="utf-8")
    (tmp_path / "OPERATIONS.md").write_text("# Operations\n\nUse skills on demand.\n", encoding="utf-8")
    (tmp_path / "NOW.md").write_text("# Now\n\nWorking on agent runtime.\n", encoding="utf-8")
    memory.append_user_memory("user-1", "User prefers concise replies.")
    memory.append_notes("session-1", "Need to test workspace context.")
    memory.write_current_task(
        "session-1",
        {
            "title": "Draft a mail",
            "intent": "chat.respond",
            "latest_reply": "Sure. Here's the draft.",
        },
    )

    bundle = workspace.context_bundle("session-1", "user-1")

    assert "## Assistant Profile" in bundle
    assert "Be calm." in bundle
    assert "## User Profile" in bundle
    assert "Call the user AD." in bundle
    assert "## Operations" in bundle
    assert "Use skills on demand." in bundle
    assert "## Current State" in bundle
    assert "Working on agent runtime." in bundle
    assert "## Memory" in bundle
    assert "User prefers concise replies." in bundle
    assert "Need to test workspace context." in bundle
    assert "## Current Task" in bundle
    assert "Draft a mail" in bundle


def test_workspace_promote_playbook_creates_and_indexes_entry(tmp_path) -> None:
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()
    workspace = WorkspaceStore(tmp_path, memory)
    workspace.initialize()

    path = workspace.promote_playbook(
        intent="gws.gmail.latest",
        skill_ids=["gws-shared", "gws-gmail"],
        commands=["gws gmail users messages list --params '{\"userId\":\"me\"}'"],
        note="Latest mail flow succeeded.",
    )

    assert path is not None
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert "# Gws Gmail Latest" in body
    assert "gws-shared, gws-gmail" in body
    assert "Latest mail flow succeeded." in body
    assert "gws.gmail.latest" in workspace.playbook_index_bundle()


def test_memory_current_task_round_trip(tmp_path) -> None:
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()

    payload = {
        "title": "Shorten the note",
        "intent": "chat.respond",
        "latest_user_request": "make it shorter",
    }
    memory.write_current_task("session-1", payload)

    assert memory.read_current_task("session-1") == payload
