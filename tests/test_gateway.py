import json
from datetime import UTC, datetime

from synapse.auth import AuthStore
from synapse.broker import CapabilityBroker
from synapse.capabilities import DEFAULT_CAPABILITY_REGISTRY
from synapse.diagnosis import DiagnosisEngine
from synapse.executors import HostExecutor, IsolatedExecutor
from synapse.gateway import Gateway
from synapse.gws import GWSBridge
from synapse.integrations import IntegrationRegistry
from synapse.introspection import RuntimeIntrospector
from synapse.memory import MemoryStore
from synapse.models import NormalizedInboundEvent
from synapse.plugins.registry import PluginRegistry
from synapse.providers import ModelRouter
from synapse.session import SessionStateMachine
from synapse.skills import SkillRegistry
from synapse.store import SQLiteStore
from synapse.workspace import WorkspaceStore


class FakeModelRouter(ModelRouter):
    def __init__(self, planner_runner=None, intent_runner=None, loop_runner=None, action_planner_runner=None) -> None:
        self._profile = None
        self.last_system_prompt = None
        self.planner_runner = planner_runner
        self.intent_runner = intent_runner
        self.loop_runner = loop_runner
        self.action_planner_runner = action_planner_runner
        self.system_prompts = []

    def resolve_profile(self):  # type: ignore[override]
        return self._profile

    async def generate(self, messages, *, system_prompt=None):  # type: ignore[override]
        self.last_system_prompt = system_prompt
        self.system_prompts.append(system_prompt)
        if system_prompt and "Intent routing runtime." in system_prompt and self.intent_runner is not None:
            return self.intent_runner(messages, system_prompt=system_prompt)
        if system_prompt and "Google Workspace planning runtime." in system_prompt and self.planner_runner is not None:
            return self.planner_runner(messages, system_prompt=system_prompt)
        if system_prompt and "Action planning runtime." in system_prompt and self.action_planner_runner is not None:
            return self.action_planner_runner(messages, system_prompt=system_prompt)
        if system_prompt and "Unified agent loop runtime." in system_prompt and self.loop_runner is not None:
            return self.loop_runner(messages, system_prompt=system_prompt)
        # Reply rendering: when execution results are in context, echo them back
        # so tests can assert on the content that went through
        for msg in messages:
            content = str(msg.get("content", ""))
            if "actions were executed" in content:
                return content
        return None


def build_gateway(
    tmp_path,
    *,
    agent_name: str = "Agent",
    assistant_instructions: str = "",
    gws_planner_instructions: str = "",
    codex_search_runner=None,
    gws_runner=None,
    planner_runner=None,
    intent_runner=None,
    loop_runner=None,
    action_planner_runner=None,
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
    model_router = FakeModelRouter(planner_runner=planner_runner, intent_runner=intent_runner, loop_runner=loop_runner, action_planner_runner=action_planner_runner)
    if gws_runner is None:
        def gws_runner(command, *, env, cwd, timeout):  # type: ignore[no-redef,no-untyped-def]
            import subprocess
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")
    gws = GWSBridge(enabled=True, env={}, workdir=str(tmp_path), runner=gws_runner)  # type: ignore[arg-type]
    introspector = RuntimeIntrospector(
        capability_registry=DEFAULT_CAPABILITY_REGISTRY,
        plugin_registry=PluginRegistry(),
        skill_registry=skills,
    )
    diagnosis_engine = DiagnosisEngine(store=store)
    return Gateway(
        store=store,
        memory=memory,
        workspace=workspace,
        skills=skills,
        broker=CapabilityBroker(),
        state_machine=SessionStateMachine(),
        host_executor=HostExecutor(
            memory,
            skills,
            store,
            integrations,
            gws,
            codex_search_runner=codex_search_runner,
            workdir=str(tmp_path),
            introspector=introspector,
            diagnosis_engine=diagnosis_engine,
        ),
        isolated_executor=IsolatedExecutor(),
        model_router=model_router,
        agent_name=agent_name,
        assistant_instructions=assistant_instructions,
        gws_planner_instructions=gws_planner_instructions,
    )


def planner_json(payload):  # type: ignore[no-untyped-def]
    return json.dumps(payload)


async def test_gateway_requires_approval_for_global_memory_write(tmp_path) -> None:
    gateway = build_gateway(tmp_path)
    event = NormalizedInboundEvent(
        adapter="telegram",
        channel_id="chat-1",
        user_id="user-1",
        message_id="message-1",
        text="/remember-global remember this",
    )

    result = await gateway.ingest(event)

    approvals = gateway.store.list_pending_approvals()
    assert result.status == "WAITING_APPROVAL"
    assert result.approval_id is not None
    assert len(approvals) == 1

    approved = await gateway.approve(result.approval_id)
    assert approved.status == "COMPLETED"
    assert "global memory updated" in approved.reply_text
    assert "remember this" in gateway.memory.global_memory_path().read_text(encoding="utf-8")


async def test_gateway_queues_follow_up_while_run_is_waiting_approval(tmp_path) -> None:
    gateway = build_gateway(tmp_path)
    first = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/remember-global needs approval",
        )
    )
    second = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-2",
            text="follow up",
        )
    )

    assert first.status == "WAITING_APPROVAL"
    assert second.queued is True
    assert gateway.store.health_snapshot()["queued_events"] == 1

    await gateway.approve(first.approval_id or "")
    runs = gateway.store.list_runs()
    assert len(runs) == 2
    assert gateway.store.health_snapshot()["queued_events"] == 0


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
    assert "If the user asks for current or live information" in gateway.model_router.last_system_prompt
    assert "Decide first whether the user wants conversation/thinking or a real-world action." in gateway.model_router.last_system_prompt
    assert "Do not let domain words alone trigger execution." in gateway.model_router.last_system_prompt


async def test_gateway_includes_runtime_prompt_overrides(tmp_path) -> None:
    gateway = build_gateway(
        tmp_path,
        assistant_instructions="Always call out when a gws command was used.",
        gws_planner_instructions="Prefer gws.inspect before gws.exec when the command shape is unclear.",
        loop_runner=lambda messages, *, system_prompt: planner_json({"status": "reply", "reply": "hi"}),
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

    await gateway._run_skill_gws_planner("my gmail inbox", skill_ids=["gws-shared"])
    planner_prompt = gateway.model_router.last_system_prompt or ""
    assert "Prefer gws.inspect before gws.exec when the command shape is unclear." in planner_prompt


async def test_gateway_agent_loop_prompt_uses_registry_summary(tmp_path) -> None:
    gateway = build_gateway(
        tmp_path,
        loop_runner=lambda messages, *, system_prompt: planner_json({"status": "reply", "reply": "ok"}),
    )

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
    assert gateway.model_router.last_system_prompt is not None
    assert "Tool registry summary:" in gateway.model_router.last_system_prompt
    assert DEFAULT_CAPABILITY_REGISTRY.get("gws.gmail.send") is not None
    assert DEFAULT_CAPABILITY_REGISTRY.get("gws.gmail.send").prompt_line() in gateway.model_router.last_system_prompt
    assert DEFAULT_CAPABILITY_REGISTRY.get("skills.read").prompt_line() in gateway.model_router.last_system_prompt


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


async def test_gateway_natural_preference_write_updates_user_memory(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="Call me AD",
        )
    )

    assert result.status == "COMPLETED"
    assert "User prefers to be called AD." in gateway.memory.read_user_memory("user-1")


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
    assert "User prefers to be called AD." in result.reply_text


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


async def test_gateway_forget_global_memory_requires_approval(tmp_path) -> None:
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

    assert result.status == "WAITING_APPROVAL"


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
    assert "Google Workspace status:" in result.reply_text


async def test_gateway_natural_gws_calendar_today_runs_without_approval(tmp_path) -> None:
    gateway = build_gateway(
        tmp_path,
        intent_runner=lambda messages, *, system_prompt: planner_json({"mode": "act"}),
        planner_runner=lambda messages, *, system_prompt: planner_json(
            {
                "status": "workflow",
                "intent": "gws.calendar.agenda",
                "renderer": "gws.calendar.agenda",
                "skill_ids": ["gws-shared", "gws-calendar-agenda"],
                "actions": [{"action": "gws.calendar.agenda", "payload": {"today": True}}],
            }
        ),
    )

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="what's on my calendar today",
        )
    )

    assert result.status == "COMPLETED"
    assert result.status == "COMPLETED"
    assert result.reply_text  # GWS result rendered


async def test_gateway_natural_gws_meeting_prep_runs_without_approval(tmp_path) -> None:
    gateway = build_gateway(
        tmp_path,
        intent_runner=lambda messages, *, system_prompt: planner_json({"mode": "act"}),
        planner_runner=lambda messages, *, system_prompt: planner_json(
            {
                "status": "workflow",
                "intent": "gws.workflow.meeting.prep",
                "renderer": "gws.workflow.meeting.prep",
                "skill_ids": ["gws-shared", "gws-workflow-meeting-prep"],
                "actions": [{"action": "gws.workflow.meeting.prep", "payload": {}}],
            }
        ),
    )

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="prep me for my next meeting",
        )
    )

    assert result.status == "COMPLETED"
    assert result.reply_text  # GWS result rendered


async def test_gateway_chat_yes_approves_pending_request(tmp_path) -> None:
    gateway = build_gateway(tmp_path)
    first = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/gws gmail send to@example.com | subject | hello",
        )
    )

    approved = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-2",
            text="yes go ahead",
        )
    )

    assert first.status == "WAITING_APPROVAL"
    assert approved.status == "COMPLETED"
    assert "Gmail sent." in approved.reply_text or "gws.gmail.send" in approved.reply_text
    assert gateway.store.list_pending_approvals() == []


async def test_gateway_chat_no_rejects_pending_request(tmp_path) -> None:
    gateway = build_gateway(tmp_path)
    first = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="/gws gmail send to@example.com | subject | hello",
        )
    )

    rejected = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-2",
            text="no cancel that",
        )
    )

    assert first.status == "WAITING_APPROVAL"
    assert rejected.status == "CANCELLED"
    assert "cancelled" in rejected.reply_text.lower()
    assert gateway.store.list_pending_approvals() == []


async def test_gateway_latest_mail_is_deterministic(tmp_path) -> None:
    def gws_runner(command, *, env, cwd, timeout):  # type: ignore[no-untyped-def]
        import json
        import subprocess

        if command[1:5] == ["gmail", "users", "messages", "list"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"messages": [{"id": "m1"}]}), stderr="")
        if command[1:5] == ["gmail", "users", "messages", "get"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "id": "m1",
                        "snippet": "hello world",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "sender@example.com"},
                                {"name": "Subject", "value": "Test"},
                                {"name": "Date", "value": "Fri"},
                            ],
                            "mimeType": "text/plain",
                            "body": {"data": "aGVsbG8gd29ybGQ="},
                        },
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")

    gateway = build_gateway(
        tmp_path,
        gws_runner=gws_runner,
        intent_runner=lambda messages, *, system_prompt: planner_json({"mode": "act"}),
        planner_runner=lambda messages, *, system_prompt: planner_json(
            {
                "status": "workflow",
                "intent": "gws.gmail.latest",
                "renderer": "gws.gmail.latest",
                "skill_ids": ["gws-shared", "gws-gmail"],
                "actions": [{"action": "gws.gmail.latest", "payload": {}}],
            }
        ),
    )

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="my last mail",
        )
    )

    assert result.status == "COMPLETED"
    assert "sender@example.com" in result.reply_text
    assert "hello world" in result.reply_text or "aGVsbG8gd29ybGQ" in result.reply_text
    playbook = tmp_path / "playbooks" / "gws.gmail.latest.md"
    assert playbook.exists()
    assert "gws.gmail.latest" in playbook.read_text(encoding="utf-8")


async def test_gateway_create_sheet_and_append_uses_compound_workflow(tmp_path) -> None:
    gateway = build_gateway(
        tmp_path,
        intent_runner=lambda messages, *, system_prompt: planner_json({"mode": "act"}),
        planner_runner=lambda messages, *, system_prompt: planner_json(
            {
                "status": "workflow",
                "intent": "gws.sheets.create_and_append",
                "renderer": "gws.sheets.append",
                "skill_ids": ["gws-shared", "gws-sheets", "gws-sheets-append"],
                "actions": [
                    {"action": "gws.sheets.create", "payload": {"title": "Budget"}},
                    {"action": "gws.sheets.append", "payload": {"spreadsheet_id": "$last.spreadsheetId", "range": "Sheet1!A:B", "values": [["item", "count"], ["flow", "1"]]}},
                    {"action": "gws.sheets.read", "payload": {"spreadsheet_id": "$last.spreadsheetId", "range": "Sheet1!A:B"}},
                ],
            }
        ),
    )

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text='create a sheet called Budget and add [["item","count"],["flow","1"]] to range Sheet1!A:B',
        )
    )

    events = gateway.store.list_run_events(result.run_id)
    planned = next(event for event in events if event["event_type"] == "workflow.planned")
    steps = planned["payload"]["steps"]

    assert result.status == "COMPLETED"
    assert len(steps) == 3
    assert steps[0]["action"]["action"] == "gws.sheets.create"
    assert steps[1]["action"]["action"] == "gws.sheets.append"
    assert steps[2]["action"]["action"] == "gws.sheets.read"


async def test_gateway_draft_mail_stays_in_chat_and_skips_gws_planner(tmp_path) -> None:
    gateway = build_gateway(tmp_path)
    seen_prompts = []

    async def generate(messages, *, system_prompt=None):  # type: ignore[override]
        seen_prompts.append(system_prompt or "")
        if system_prompt and "Intent routing runtime." in system_prompt:
            return planner_json({"mode": "chat"})
        return "Sure. Here is the draft."

    gateway.model_router.generate = generate  # type: ignore[method-assign]

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="draft a mail for Apoorva about ghazals and show me the draft first",
        )
    )

    assert result.status == "COMPLETED"
    assert result.reply_text == "Sure. Here is the draft."
    assert not any("Google Workspace planning runtime." in prompt for prompt in seen_prompts)


async def test_gateway_completed_chat_updates_current_task(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    async def generate(messages, *, system_prompt=None):  # type: ignore[override]
        if system_prompt and "Intent routing runtime." in system_prompt:
            return planner_json({"mode": "chat"})
        return "Sure. Here is the shorter draft."

    gateway.model_router.generate = generate  # type: ignore[method-assign]

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="make it shorter",
        )
    )

    task = gateway.memory.read_current_task("telegram__chat-1__user-1")
    assert result.status == "COMPLETED"
    assert task is not None
    assert task["mode"] == "chat"
    assert task["latest_user_request"] == "make it shorter"
    assert task["latest_reply"] == "Sure. Here is the shorter draft."


async def test_gateway_chat_task_stores_skill_hints_for_follow_ups(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    async def generate(messages, *, system_prompt=None):  # type: ignore[override]
        if system_prompt and "Intent routing runtime." in system_prompt:
            return planner_json({"mode": "chat"})
        return "Sure. Here is the draft."

    gateway.model_router.generate = generate  # type: ignore[method-assign]

    await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="draft a mail for Apoorva about ghazals",
        )
    )

    task = gateway.memory.read_current_task("telegram__chat-1__user-1")
    assert task is not None
    assert any(str(skill_id).startswith("gws-") for skill_id in task["skill_ids"])


async def test_gateway_short_operational_follow_up_routes_to_act_without_router(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    gateway.memory.write_current_task(
        "telegram__chat-1__user-1",
        {
            "title": "Send the drafted email",
            "intent": "gws.gmail.send",
            "mode": "act",
            "latest_user_request": "send it to apoorva@example.com",
            "latest_reply": "Reply yes and I will send it.",
            "skill_ids": ["gws-shared", "gws-gmail"],
        },
    )

    event = NormalizedInboundEvent(
        adapter="telegram",
        channel_id="chat-1",
        user_id="user-1",
        message_id="message-2",
        text="yes please",
    )

    assert await gateway._intent_mode(event, session_key="telegram__chat-1__user-1") == "act"


async def test_gateway_chat_follow_up_preserves_existing_task_context(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    gateway.memory.write_current_task(
        "telegram__chat-1__user-1",
        {
            "title": "Send the drafted email",
            "intent": "gws.gmail.send",
            "mode": "act",
            "latest_user_request": "send it to apoorva@example.com",
            "latest_reply": "Reply yes and I will send it.",
            "skill_ids": ["gws-shared", "gws-gmail", "gws-gmail-send"],
            "actions": ["gws.gmail.send"],
        },
    )

    async def generate(messages, *, system_prompt=None):  # type: ignore[override]
        if system_prompt and "Intent routing runtime." in system_prompt:
            return planner_json({"mode": "chat"})
        return "Not yet confirmed."

    gateway.model_router.generate = generate  # type: ignore[method-assign]

    await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-2",
            text="done?",
        )
    )

    task = gateway.memory.read_current_task("telegram__chat-1__user-1")
    assert task is not None
    assert task["title"] == "Send the drafted email"
    assert task["intent"] == "gws.gmail.send"
    assert task["mode"] == "act"
    assert "gws-gmail-send" in task["skill_ids"]
    assert "gws.gmail.send" in task["actions"]


async def test_gateway_planner_receives_current_task_context(tmp_path) -> None:
    captured = {}

    def planner_runner(messages, *, system_prompt):  # type: ignore[no-untyped-def]
        captured["messages"] = messages
        return planner_json({"status": "not_gws", "skill_ids": ["gws-shared"]})

    gateway = build_gateway(
        tmp_path,
        planner_runner=planner_runner,
        intent_runner=lambda messages, *, system_prompt: planner_json({"mode": "act"}),
    )

    gateway.memory.write_current_task(
        "telegram__chat-1__user-1",
        {
            "title": "Draft a mail for Apoorva",
            "intent": "chat.respond",
            "latest_user_request": "draft a mail for Apoorva about ghazals",
            "latest_reply": "Sure. Here is the draft.",
            "skill_ids": ["gws-shared", "gws-gmail"],
        },
    )

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="send it to apoorva@example.com",
        )
    )

    assert result.status == "COMPLETED"
    assert "messages" in captured
    planner_message = captured["messages"][0]["content"]
    assert "Current task:" in planner_message
    assert "Draft a mail for Apoorva" in planner_message
    assert "Sure. Here is the draft." in planner_message


async def test_gateway_includes_attachment_summary_in_model_messages(tmp_path) -> None:
    gateway = build_gateway(tmp_path)
    captured = {}

    async def generate(messages, *, system_prompt=None):  # type: ignore[override]
        captured["messages"] = messages
        captured["system_prompt"] = system_prompt
        return "got it"

    gateway.model_router.generate = generate  # type: ignore[method-assign]

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


async def test_gateway_help_command_returns_capabilities(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="what can you do",
        )
    )

    assert result.status == "COMPLETED"
    assert "capabilities.read" in result.reply_text


async def test_gateway_agent_loop_rejects_plain_reply_for_operational_follow_up(tmp_path) -> None:
    calls = {"count": 0}

    def loop_runner(messages, *, system_prompt):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 1:
            return planner_json({"status": "reply", "reply": "Sending it now."})
        if calls["count"] == 2:
            return planner_json(
                {
                    "status": "tool_calls",
                    "intent": "memory.read",
                    "renderer": "memory.read",
                    "skill_ids": [],
                    "tool_calls": [
                        {
                            "action": "memory.read",
                            "payload": {
                                "scope": "all",
                            },
                        }
                    ],
                }
            )
        return planner_json({"status": "reply", "reply": "done"})

    gateway = build_gateway(tmp_path, loop_runner=loop_runner)
    gateway.memory.write_current_task(
        "telegram__chat-1__user-1",
        {
            "title": "Load the memory snapshot",
            "intent": "memory.read",
            "mode": "act",
            "latest_user_request": "show me what you remember",
            "latest_reply": "Ready to load the snapshot.",
            "skill_ids": [],
            "actions": ["memory.read"],
        },
    )

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="do it",
        )
    )

    events = gateway.store.list_run_events(result.run_id)
    assert result.status == "COMPLETED"
    assert result.reply_text == "done"
    assert calls["count"] == 3
    assert any(
        event["event_type"] == "model.turn.completed"
        and event["payload"]["directive"]["status"] == "reply"
        for event in events
    )


async def test_gateway_natural_language_reminder_creates_schedule(tmp_path) -> None:
    gateway = build_gateway(tmp_path)

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="message me in 5 mins to stretch",
            occurred_at=datetime(2026, 3, 5, 22, 0, tzinfo=UTC),
        )
    )

    reminders = gateway.store.list_reminders()
    assert result.status == "COMPLETED"
    assert "reminder.create" in result.reply_text
    assert reminders
    assert reminders[0].message == "stretch"


async def test_gateway_routes_latest_query_through_web_search(tmp_path) -> None:
    gateway = build_gateway(
        tmp_path,
        codex_search_runner=lambda query: {"query": query, "answer": "Latest answer with sources"},
    )

    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            text="what is the latest bitcoin news",
        )
    )

    assert result.status == "COMPLETED"
    assert "web.search" in result.reply_text
    assert "Latest answer with sources" in result.reply_text


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


async def test_action_planner_routes_self_describe(tmp_path) -> None:
    """When the intent router says 'act' and GWS returns None,
    the action planner should route self-awareness queries to self.* actions."""

    def intent_runner(messages, *, system_prompt=None):  # type: ignore[no-untyped-def]
        return json.dumps({"mode": "act"})

    def action_planner_runner(messages, *, system_prompt=None):  # type: ignore[no-untyped-def]
        return json.dumps({
            "status": "workflow",
            "intent": "self.describe",
            "renderer": "default",
            "actions": [
                {"action": "self.describe", "payload": {}},
                {"action": "self.capabilities", "payload": {}},
            ],
        })

    gateway = build_gateway(
        tmp_path,
        intent_runner=intent_runner,
        action_planner_runner=action_planner_runner,
    )
    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="msg-self",
            text="What are you and what can you do?",
        )
    )
    assert result.status == "COMPLETED"
    # The run should have executed self.describe and self.capabilities actions
    events = gateway.store.list_run_events(result.run_id)
    action_events = [e for e in events if e["event_type"] == "workflow.step.completed"]
    assert len(action_events) == 2
    first_result = action_events[0]["payload"]["result"]
    assert first_result["action"] == "self.describe"
    assert first_result["success"] is True
    assert "Synapse" in first_result["detail"]
    second_result = action_events[1]["payload"]["result"]
    assert second_result["action"] == "self.capabilities"
    assert second_result["success"] is True
    assert len(second_result["artifacts"]["capabilities"]) > 10


async def test_action_planner_routes_diagnosis(tmp_path) -> None:
    """Action planner routes diagnosis queries to diagnosis.report."""

    def intent_runner(messages, *, system_prompt=None):  # type: ignore[no-untyped-def]
        return json.dumps({"mode": "act"})

    def action_planner_runner(messages, *, system_prompt=None):  # type: ignore[no-untyped-def]
        return json.dumps({
            "status": "workflow",
            "intent": "diagnosis.report",
            "renderer": "default",
            "actions": [{"action": "diagnosis.report", "payload": {}}],
        })

    gateway = build_gateway(
        tmp_path,
        intent_runner=intent_runner,
        action_planner_runner=action_planner_runner,
    )
    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="msg-diag",
            text="Run a diagnosis on yourself",
        )
    )
    assert result.status == "COMPLETED"
    events = gateway.store.list_run_events(result.run_id)
    action_events = [e for e in events if e["event_type"] == "workflow.step.completed"]
    assert len(action_events) == 1
    diag_result = action_events[0]["payload"]["result"]
    assert diag_result["action"] == "diagnosis.report"
    assert diag_result["success"] is True
    assert "report" in diag_result["artifacts"]


async def test_action_planner_no_match_falls_to_chat(tmp_path) -> None:
    """When action planner returns no_match, falls through to chat.respond."""

    def intent_runner(messages, *, system_prompt=None):  # type: ignore[no-untyped-def]
        return json.dumps({"mode": "act"})

    def action_planner_runner(messages, *, system_prompt=None):  # type: ignore[no-untyped-def]
        return json.dumps({"status": "no_match"})

    gateway = build_gateway(
        tmp_path,
        intent_runner=intent_runner,
        action_planner_runner=action_planner_runner,
    )
    result = await gateway.ingest(
        NormalizedInboundEvent(
            adapter="telegram",
            channel_id="chat-1",
            user_id="user-1",
            message_id="msg-nomatch",
            text="tell me a joke about python",
        )
    )
    assert result.status == "COMPLETED"
