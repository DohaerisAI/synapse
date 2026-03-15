import json

from synapse.approvals import ApprovalManager
from synapse.auth import AuthStore
from synapse.executors import HostExecutor, IsolatedExecutor
from synapse.gateway import Gateway
from synapse.gws import GWSBridge
from synapse.integrations import IntegrationRegistry
from synapse.memory import MemoryStore
from synapse.models import NormalizedInboundEvent
from synapse.providers import LLMResponse, ModelRouter
from synapse.session import SessionStateMachine
from synapse.skills import SkillRegistry
from synapse.store import SQLiteStore
from synapse.tools.builtins import register_builtin_tools
from synapse.tools.registry import ToolRegistry
from synapse.workspace import WorkspaceStore


class FakeModelRouter(ModelRouter):
    def __init__(self, chat_runner=None) -> None:
        self._profile = None
        self.last_system_prompt = None
        self.chat_runner = chat_runner
        self.system_prompts = []
        self.last_messages = None

    def resolve_profile(self):  # type: ignore[override]
        return self._profile

    async def generate(self, messages, *, system_prompt=None):  # type: ignore[override]
        self.last_system_prompt = system_prompt
        self.system_prompts.append(system_prompt)
        return None

    async def chat(self, messages, *, system_prompt=None, tools=None, sink=None):  # type: ignore[override]
        import asyncio

        self.last_system_prompt = system_prompt
        self.system_prompts.append(system_prompt)
        self.last_messages = messages
        if self.chat_runner is not None:
            result = self.chat_runner(messages, system_prompt=system_prompt, tools=tools)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        return LLMResponse(text="OK")


def build_gateway(
    tmp_path,
    *,
    agent_name: str = "Agent",
    assistant_instructions: str = "",
    gws_planner_instructions: str = "",
    codex_search_runner=None,
    gws_runner=None,
    chat_runner=None,
) -> Gateway:
    auth = AuthStore(tmp_path / "auth.json", tmp_path / "config.json", env={})
    memory = MemoryStore(tmp_path / "memory")
    memory.initialize()
    workspace = WorkspaceStore(tmp_path, memory)
    workspace.initialize()
    skill_specs = [
        ("assistant", "Assistant", "test", []),
        ("gws-shared", "gws-shared", "gws shared", ["gws"]),
        ("gws-gmail", "gws-gmail", "gmail and mail operations", ["gws"]),
        ("gws-calendar-agenda", "gws-calendar-agenda", "calendar agenda helper", ["gws"]),
        ("gws-workflow-meeting-prep", "gws-workflow-meeting-prep", "meeting prep helper", ["gws"]),
        ("gws-sheets", "gws-sheets", "google sheets operations", ["gws"]),
        ("gws-sheets-append", "gws-sheets-append", "append rows to sheets", ["gws"]),
    ]
    for skill_id, name, description, capabilities in skill_specs:
        skill_dir = tmp_path / "skills" / skill_id
        skill_dir.mkdir(parents=True)
        (skill_dir / "manifest.json").write_text(
            json.dumps({"id": skill_id, "name": name, "description": description, "capabilities": capabilities}),
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(f"Skill: {name}", encoding="utf-8")
    skills = SkillRegistry(tmp_path / "skills")
    skills.load()
    store = SQLiteStore(tmp_path / "var" / "runtime.sqlite3")
    store.initialize()
    integrations = IntegrationRegistry(tmp_path / "integrations", skills_dir=tmp_path / "skills", boot_path=tmp_path / "BOOT.md", env={})
    integrations.initialize()
    model_router = FakeModelRouter(chat_runner=chat_runner)
    if gws_runner is None:
        def gws_runner(command, *, env, cwd, timeout):  # type: ignore[no-redef,no-untyped-def]
            import subprocess
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")
    gws = GWSBridge(enabled=True, env={}, workdir=str(tmp_path), runner=gws_runner)  # type: ignore[arg-type]

    tool_registry = ToolRegistry()
    register_builtin_tools(tool_registry)
    approval_manager = ApprovalManager(tmp_path / "approvals.json")

    host_executor = HostExecutor(
        memory,
        skills,
        store,
        integrations,
        gws,
        codex_search_runner=codex_search_runner,
        workdir=str(tmp_path),
    )

    return Gateway(
        store=store,
        memory=memory,
        workspace=workspace,
        skills=skills,
        state_machine=SessionStateMachine(),
        model_router=model_router,
        agent_name=agent_name,
        assistant_instructions=assistant_instructions,
        gws_planner_instructions=gws_planner_instructions,
        tool_registry=tool_registry,
        approval_manager=approval_manager,
        host_executor=host_executor,
        isolated_executor=IsolatedExecutor(),
    )


# --- Slash command tests ---


async def test_gateway_memory_command_returns_snapshot(tmp_path) -> None:
    gateway = build_gateway(tmp_path)
    gateway.memory.append_user_memory("user-1", "User prefers to be called AD.")

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/memory",
        )
    )

    assert result.status == "COMPLETED"
    assert "memory.read" in result.reply_text


async def test_gateway_remember_global_writes_memory(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/remember-global remember this",
        )
    )

    assert result.status == "COMPLETED"
    assert "global memory updated" in result.reply_text
    assert "remember this" in gateway.memory.global_memory_path().read_text(encoding="utf-8")


async def test_gateway_forget_user_memory_removes_entry(tmp_path) -> None:
    gateway = build_gateway(tmp_path)
    gateway.memory.append_user_memory("user-1", "User prefers to be called AD.")

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/forget-user User prefers to be called AD.",
        )
    )

    assert result.status == "COMPLETED"
    assert "user memory removed" in result.reply_text
    assert "called AD" not in gateway.memory.read_user_memory("user-1")


async def test_gateway_forget_global_memory_removes_entry(tmp_path) -> None:
    gateway = build_gateway(tmp_path)
    gateway.memory.append_global_memory("Remember the team handbook.")

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/forget-global Remember the team handbook.",
        )
    )

    assert result.status == "COMPLETED"
    assert "global memory removed" in result.reply_text


async def test_gateway_gws_status_runs_without_approval(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/gws status",
        )
    )

    assert result.status == "COMPLETED"
    assert "gws.auth.status" in result.reply_text


async def test_gateway_gws_gmail_send_executes(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/gws gmail send to@example.com | subject | hello",
        )
    )

    assert result.status == "COMPLETED"
    assert "gws.gmail.send" in result.reply_text


async def test_gateway_supports_explicit_search_command(tmp_path) -> None:
    gateway = build_gateway(
        tmp_path,
        codex_search_runner=lambda query: {"query": query, "answer": f"searched: {query}"},
    )

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/search latest tesla deliveries",
        )
    )

    assert result.status == "COMPLETED"
    assert "web.search" in result.reply_text
    assert "latest tesla deliveries" in result.reply_text


# --- React loop tests ---


async def test_gateway_includes_agent_name_in_system_prompt(tmp_path) -> None:
    gateway = build_gateway(tmp_path, agent_name="Nora")

    await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="what is your name?",
        )
    )

    assert gateway.model_router.last_system_prompt is not None
    assert "Your name is Nora." in gateway.model_router.last_system_prompt


async def test_gateway_includes_runtime_prompt_overrides(tmp_path) -> None:
    gateway = build_gateway(
        tmp_path,
        assistant_instructions="Always call out when a gws command was used.",
    )

    await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="what is your name?",
        )
    )
    assert "Always call out when a gws command was used." in gateway.model_router.last_system_prompt


async def test_gateway_loads_user_memory_into_system_prompt(tmp_path) -> None:
    gateway = build_gateway(tmp_path, agent_name="Nora")
    gateway.memory.append_user_memory("user-1", "User prefers to be called AD.")

    await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="who am i?",
        )
    )

    assert gateway.model_router.last_system_prompt is not None
    assert "User prefers to be called AD." in gateway.model_router.last_system_prompt


async def test_gateway_react_loop_returns_model_reply(tmp_path) -> None:
    """Non-slash messages go through the react loop and return the model reply."""

    def chat_runner(messages, *, system_prompt=None, tools=None):  # type: ignore[no-untyped-def]
        return LLMResponse(text="Hello from the react loop!")

    gateway = build_gateway(tmp_path, chat_runner=chat_runner)

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="hello there",
        )
    )

    assert result.status == "COMPLETED"
    assert result.reply_text == "Hello from the react loop!"


async def test_gateway_includes_attachment_summary_in_model_messages(tmp_path) -> None:
    captured = {}

    def chat_runner(messages, *, system_prompt=None, tools=None):  # type: ignore[no-untyped-def]
        captured["messages"] = messages
        captured["system_prompt"] = system_prompt
        return LLMResponse(text="got it")

    gateway = build_gateway(tmp_path, chat_runner=chat_runner)

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="User uploaded: document report.xlsx",
            metadata={
                "attachments": [
                    {
                        "kind": "document",
                        "file_name": "report.xlsx",
                        "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    }
                ]
            },
        )
    )

    assert result.reply_text == "got it"
    assert any(
        "Inbound attachments:\ndocument (report.xlsx)" in message["content"]
        for message in captured["messages"]
    )
    assert captured["messages"][0]["attachments"][0]["file_name"] == "report.xlsx"


# --- Queuing tests ---


async def test_gateway_queues_follow_up_while_run_is_active(tmp_path) -> None:
    """When a run is active, subsequent messages are queued."""
    import asyncio

    gate = asyncio.Event()
    captured = {}

    async def slow_chat(messages, *, system_prompt=None, tools=None):  # type: ignore[no-untyped-def]
        await gate.wait()
        return LLMResponse(text="first done")

    gateway = build_gateway(tmp_path, chat_runner=slow_chat)

    async def first_ingest():
        return await gateway.ingest(
            NormalizedInboundEvent(
                adapter="telegram",
                channel_id="chat-1",
                user_id="user-1",
                message_id="message-1",
                text="first message",
            )
        )

    task = asyncio.create_task(first_ingest())
    await asyncio.sleep(0.05)

    second = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-2",
            text="follow up",
        )
    )

    assert second.queued is True
    assert gateway.store.health_snapshot()["queued_events"] == 1

    gate.set()
    first_result = await task
    assert first_result.status == "COMPLETED"
