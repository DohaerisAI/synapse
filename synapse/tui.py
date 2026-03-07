from __future__ import annotations

from dataclasses import asdict, is_dataclass

from pydantic import BaseModel
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static, Switch, TabbedContent, TabPane

from .envfile import CONFIG_FIELDS, write_env_file
from .runtime import build_runtime


def run_tui(
    runtime: Any,
    *,
    refresh_interval: float = 2.0,
    once: bool = False,
) -> None:
    if once:
        print(render_tui(runtime))
        return
    RuntimeTuiApp(runtime, refresh_interval=refresh_interval).run()


def render_tui(runtime: Any) -> str:
    snapshot = runtime.tui_snapshot()
    auth = snapshot["auth"]
    telegram = snapshot["telegram"]
    health = snapshot["health"]
    return "\n".join(
        [
            "Agent Runtime TUI",
            f"Resolved model: {resolved_provider(auth)}",
            f"Auth source: {resolved_source(auth)}",
            f"Telegram: {telegram['status']}",
            f"GWS: {'enabled' if snapshot['gws']['enabled'] else 'disabled'} / {'installed' if snapshot['gws']['installed'] else 'missing'}",
            f"Workspace files: {len(snapshot['workspace']['files'])}",
            f"Playbooks: {len(snapshot['workspace']['playbooks'])}",
            f"Heartbeat: {heartbeat_summary(snapshot['heartbeat'])}",
            f"Approvals: {health['pending_approvals']}",
            f"Queued events: {health['queued_events']}",
        ]
    )


class RuntimeTuiApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        background: #111111;
        color: #f2f2f2;
    }

    Header {
        background: #111111;
        color: #f2f2f2;
    }

    Footer {
        background: #1b1b1b;
    }

    TabbedContent {
        background: #111111;
    }

    TabPane {
        padding: 1 2;
    }

    #status-strip {
        height: 4;
        border: round #3d3d3d;
        background: #171717;
        color: #f6f6f6;
        padding: 1 2;
        margin: 0 0 1 0;
    }

    .card {
        border: round #3a3a3a;
        background: #171717;
        color: #f3f3f3;
        padding: 1 2;
        margin: 0 1 1 0;
        min-height: 8;
    }

    .soft {
        color: #a7a7a7;
    }

    .section-title {
        color: #ffffff;
        text-style: bold;
        margin: 0 0 1 0;
    }

    #control-grid {
        height: auto;
    }

    #control-grid > .card {
        width: 1fr;
    }

    #control-actions {
        height: auto;
        margin: 0 0 1 0;
    }

    #configure-layout {
        layout: vertical;
    }

    .config-section {
        border: round #333333;
        background: #151515;
        padding: 1 2;
        margin: 0 0 1 0;
    }

    .config-row {
        height: auto;
        margin: 0 0 1 0;
    }

    .label {
        width: 24;
        color: #d0d0d0;
    }

    Input {
        width: 1fr;
        background: #101010;
        border: round #3a3a3a;
        color: #f5f5f5;
    }

    Switch {
        margin: 0 0 0 1;
    }

    Button {
        margin: 0 1 0 0;
    }

    #config-status {
        min-height: 3;
    }

    #activity-layout {
        height: 1fr;
    }

    #runs-panel, #approvals-panel {
        width: 1fr;
    }

    DataTable {
        height: 1fr;
        border: round #333333;
        background: #121212;
    }

    #qr-box {
        min-height: 18;
        overflow: auto;
    }

    #qr-help {
        min-height: 6;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reload_runtime", "Reload Runtime"),
        ("s", "save_config", "Save Config"),
        ("a", "approve_selected", "Approve Selected"),
    ]

    def __init__(self, runtime: Any, *, refresh_interval: float = 2.0) -> None:
        super().__init__()
        self.runtime = runtime
        self.refresh_interval = refresh_interval
        self.root_path = runtime.config.paths.root

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="[]")
        with TabbedContent():
            with TabPane("Control", id="control-pane"):
                yield Static(id="status-strip")
                with Horizontal(id="control-grid"):
                    yield Static(id="summary-card", classes="card")
                    yield Static(id="auth-card", classes="card")
                    yield Static(id="adapter-card", classes="card")
                with Horizontal():
                    yield Static(id="skill-card", classes="card")
                    yield Static(id="gws-card", classes="card")
                    yield Static(id="config-card", classes="card")
                yield Static(id="usage-card", classes="card")
                with Horizontal(id="control-actions"):
                    yield Button("Save", id="save-config", variant="primary")
                    yield Button("Reload", id="reload-runtime")
                    yield Button("Start", id="start-services")
                    yield Button("Stop", id="stop-services")
                    yield Button("Approve Selected", id="approve-selected", variant="warning")
                yield Static(id="config-status", classes="card")
            with TabPane("Configure", id="configure-pane"):
                with Vertical(id="configure-layout"):
                    with Vertical(classes="config-section"):
                        yield Label("Identity", classes="section-title")
                        yield self._config_row("Agent name", "AGENT_NAME")
                        yield self._config_row("Extra instructions", "AGENT_EXTRA_INSTRUCTIONS")
                    with Vertical(classes="config-section"):
                        yield Label("Telegram", classes="section-title")
                        yield self._config_row("Bot token", "TELEGRAM_BOT_TOKEN", password=True)
                        yield self._config_row("Polling enabled", "TELEGRAM_POLLING_ENABLED", switch=True)
                        yield self._config_row("Poll interval", "TELEGRAM_POLL_INTERVAL")
                    with Vertical(classes="config-section"):
                        yield Label("Google Workspace", classes="section-title")
                        yield self._config_row("Enabled", "GWS_ENABLED", switch=True)
                        yield self._config_row("Binary", "GWS_BINARY")
                        yield self._config_row("Allowed services", "GWS_ALLOWED_SERVICES")
                        yield self._config_row("Planner instructions", "GWS_PLANNER_EXTRA_INSTRUCTIONS")
                    with Vertical(classes="config-section"):
                        yield Label("Heartbeat", classes="section-title")
                        yield self._config_row("Enabled", "HEARTBEAT_ENABLED", switch=True)
                        yield self._config_row("Every minutes", "HEARTBEAT_EVERY_MINUTES")
                        yield self._config_row("Target", "HEARTBEAT_TARGET")
                        yield self._config_row("Ack mode", "HEARTBEAT_ACK_MODE")
                        yield self._config_row("Active hours", "HEARTBEAT_ACTIVE_HOURS")
                        yield self._config_row("Max chars", "HEARTBEAT_MAX_CHARS")
                    with Vertical(classes="config-section"):
                        yield Label("Model / Auth", classes="section-title")
                        yield self._config_row("Codex model", "CODEX_MODEL")
                        yield self._config_row("Codex auth file", "CODEX_AUTH_FILE")
                        yield self._config_row("Codex transport", "CODEX_TRANSPORT")
            with TabPane("Activity", id="activity-pane"):
                with Horizontal(id="activity-layout"):
                    with Vertical(id="runs-panel"):
                        yield Label("Runs", classes="section-title")
                        yield DataTable(id="runs-table")
                    with Vertical(id="approvals-panel"):
                        yield Label("Approvals", classes="section-title")
                        yield DataTable(id="approvals-table")
            with TabPane("Integrations", id="integrations-pane"):
                yield Static(id="integrations-summary", classes="card")
                yield DataTable(id="integrations-table")
        yield Footer()

    def on_mount(self) -> None:
        self.runtime.start_background_services()
        self.query_one(DataTable, expect_type=DataTable)
        self._setup_tables()
        self._populate_config_inputs()
        self.refresh_view()
        self.set_interval(self.refresh_interval, self.refresh_view)

    def on_unmount(self) -> None:
        self.runtime.stop_background_services()

    def _setup_tables(self) -> None:
        runs_table = self.query_one("#runs-table", DataTable)
        runs_table.cursor_type = "row"
        runs_table.add_columns("Run ID", "Adapter", "State", "Session", "Updated")
        approvals_table = self.query_one("#approvals-table", DataTable)
        approvals_table.cursor_type = "row"
        approvals_table.add_columns("Approval ID", "Run ID", "Action", "Status", "Created")
        integrations_table = self.query_one("#integrations-table", DataTable)
        integrations_table.cursor_type = "row"
        integrations_table.add_columns("ID", "Kind", "Status", "Required Env", "Last Error")

    def refresh_view(self) -> None:
        snapshot = self.runtime.tui_snapshot()
        self._update_overview(snapshot)
        self._update_runs(snapshot)
        self._update_approvals(snapshot)
        self._update_integrations(snapshot)

    def _update_overview(self, snapshot: dict[str, Any]) -> None:
        health = snapshot["health"]
        auth = snapshot["auth"]
        telegram = snapshot["telegram"]
        gws = snapshot["gws"]
        services_note = snapshot.get("background_services_note")
        heartbeat = snapshot["heartbeat"]
        self.query_one("#status-strip", Static).update(
            "\n".join(
                [
                    f"Model  {resolved_provider(auth)}   [{resolved_source(auth)}]",
                    f"Telegram  {telegram['status']}   Approvals  {health['pending_approvals']}   Queue  {health['queued_events']}",
                    f"Heartbeat  {heartbeat_summary(heartbeat)}",
                    f"GWS  {'enabled' if gws['enabled'] else 'disabled'} / {'installed' if gws['installed'] else 'missing'}",
                    services_note or ("Services owned by this process." if snapshot.get("background_services_owned") else "Services are not running in this process."),
                ]
            )
        )
        self.query_one("#summary-card", Static).update(
            "\n".join(
                [
                    "Runtime",
                    "",
                    f"Agent name: {snapshot['agent_name']}",
                    f"Pending approvals: {health['pending_approvals']}",
                    f"Queued events: {health['queued_events']}",
                    f"Runs by state: {format_mapping(health['runs_by_state'])}",
                    f"Heartbeat: {heartbeat_summary(heartbeat)}",
                    f"Integrations: {len(snapshot.get('integrations', []))}",
                    f"GWS: {'enabled' if gws['enabled'] else 'disabled'} / {'auth ready' if gws['auth_available'] else 'auth missing'}",
                ]
            )
        )
        self.query_one("#auth-card", Static).update(
            "\n".join(
                [
                    "Auth",
                    "",
                    f"Resolved: {resolved_provider(auth)}",
                    f"Source: {resolved_source(auth)}",
                    f"Transport: {resolved_transport(auth)}",
                    f"Local profiles: {auth['sources']['local_profiles']['count']}",
                    f"Codex CLI: {'yes' if auth['sources']['codex_cli']['available'] else 'no'}",
                ]
            )
        )
        self.query_one("#adapter-card", Static).update(
            "\n".join(
                [
                    "Channels",
                    "",
                    f"Telegram: {telegram['status']} polling={telegram['polling_enabled']}",
                    "Only Telegram is enabled in this build.",
                ]
            )
        )
        self.query_one("#skill-card", Static).update(
            "Skills\n\n" + ("\n".join(snapshot["skills"]) if snapshot["skills"] else "none")
        )
        self.query_one("#gws-card", Static).update(
            "\n".join(
                [
                    "Google Workspace",
                    "",
                    f"Enabled: {gws['enabled']}",
                    f"Installed: {gws['installed']}",
                    f"Auth: {gws['credential_source']}",
                    f"Services: {', '.join(gws['allowed_services'])}",
                ]
            )
        )
        self.query_one("#config-card", Static).update(
            "\n".join(
                [
                    "Files",
                    "",
                    f"Root: {self.root_path}",
                    f"Env file: {self.root_path / '.env.local'}",
                    f"SQLite: {self.runtime.config.paths.sqlite_path}",
                ]
            )
        )
        self.query_one("#usage-card", Static).update(self._usage_help(telegram))

    def _update_integrations(self, snapshot: dict[str, Any]) -> None:
        integrations = snapshot.get("integrations", [])
        self.query_one("#integrations-summary", Static).update(
            "\n".join(
                [
                    "Integrations",
                    "",
                    f"Count: {len(integrations)}",
                    "BOOT.md:",
                    "\n".join(snapshot.get("boot_tasks", [])) or "none",
                ]
            )
        )
        table = self.query_one("#integrations-table", DataTable)
        table.clear(columns=False)
        for integration in integrations:
            row = to_dict(integration)
            table.add_row(
                row["integration_id"],
                row["kind"],
                row["status"],
                ", ".join(row.get("required_env", [])) or "-",
                str(row.get("last_error") or "-"),
            )

    def _update_runs(self, snapshot: dict[str, Any]) -> None:
        table = self.query_one("#runs-table", DataTable)
        table.clear(columns=False)
        for run in snapshot["runs"]:
            row = to_dict(run)
            table.add_row(row["run_id"][:8], row["adapter"], row["state"], row["session_key"], row["updated_at"])

    def _update_approvals(self, snapshot: dict[str, Any]) -> None:
        table = self.query_one("#approvals-table", DataTable)
        table.clear(columns=False)
        for approval in snapshot["approvals"]:
            row = to_dict(approval)
            table.add_row(row["approval_id"][:8], row["run_id"][:8], row["action_name"], row["status"], row["created_at"])

    def _populate_config_inputs(self) -> None:
        config_values = self.runtime.tui_snapshot()["config"]
        for field in CONFIG_FIELDS:
            value = str(config_values.get(field, ""))
            widget = self.query_one(f"#field-{field}")
            if isinstance(widget, Switch):
                widget.value = value.lower() in {"1", "true", "yes", "on"}
            elif isinstance(widget, Input):
                widget.value = value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-config":
            self.action_save_config()
        elif event.button.id == "reload-runtime":
            self.action_reload_runtime()
        elif event.button.id == "start-services":
            self.runtime.start_background_services()
            self.refresh_view()
        elif event.button.id == "stop-services":
            self.runtime.stop_background_services()
            self.refresh_view()
        elif event.button.id == "approve-selected":
            self.action_approve_selected()

    def action_save_config(self) -> None:
        values: dict[str, str] = {}
        for field in CONFIG_FIELDS:
            widget = self.query_one(f"#field-{field}")
            if isinstance(widget, Switch):
                values[field] = "1" if widget.value else "0"
            elif isinstance(widget, Input):
                values[field] = widget.value
        env_path = Path(self.root_path) / ".env.local"
        write_env_file(env_path, values)
        self.query_one("#config-status", Static).update(f"Saved {env_path}")

    def action_reload_runtime(self) -> None:
        self.runtime.stop_background_services()
        self.runtime = build_runtime(Path(self.root_path))
        self.runtime.start_background_services()
        self._populate_config_inputs()
        self.refresh_view()
        self.query_one("#config-status", Static).update("Runtime reloaded from environment files.")

    def action_approve_selected(self) -> None:
        table = self.query_one("#approvals-table", DataTable)
        if table.row_count == 0:
            self.query_one("#config-status", Static).update("No pending approvals.")
            return
        row_index = table.cursor_row
        approval_id = table.get_row_at(row_index)[0]
        full_id = next(
            (
                to_dict(approval)["approval_id"]
                for approval in self.runtime.tui_snapshot()["approvals"]
                if to_dict(approval)["approval_id"].startswith(approval_id)
            ),
            None,
        )
        if not full_id:
            self.query_one("#config-status", Static).update("Unable to resolve approval id.")
            return
        result = self.runtime.gateway.approve(full_id)
        self.runtime.deliver_result(result)
        self.refresh_view()
        self.query_one("#config-status", Static).update(f"Approved {full_id[:8]}.")

    def _config_row(self, label: str, field: str, *, switch: bool = False, password: bool = False) -> Horizontal:
        if switch:
            return Horizontal(
                Label(label, classes="label"),
                Switch(value=False, id=f"field-{field}"),
                classes="config-row",
            )
        return Horizontal(
            Label(label, classes="label"),
            Input(id=f"field-{field}", password=password),
            classes="config-row",
        )

    def _usage_help(self, telegram: dict[str, Any]) -> str:
        lines = [
            "How To Test",
            "",
            "Telegram:",
            "Send a message from any Telegram account to your bot username.",
            f"Bot configured: {'yes' if telegram['configured'] else 'no'} | polling: {'on' if telegram['polling_enabled'] else 'off'}",
            "",
            "Google Workspace:",
            "Use /gws status, /gws gmail search ..., /gws calendar agenda, /gws drive search ...,",
            "or natural requests like 'my last mail' and 'what's on my calendar today'.",
            "Every GWS action still requires approval, and you can also approve with yes/go ahead in chat.",
            "",
            "Channel scope:",
            "Telegram is the only live channel in this runtime.",
        ]
        return "\n".join(lines)


def resolved_provider(snapshot: dict[str, Any]) -> str:
    resolved = snapshot.get("resolved")
    if not resolved:
        return "unresolved"
    return f"{resolved['provider']} / {resolved['model']}"


def resolved_source(snapshot: dict[str, Any]) -> str:
    resolved = snapshot.get("resolved")
    if not resolved:
        return "none"
    return str(resolved["source"])


def resolved_transport(snapshot: dict[str, Any]) -> str:
    resolved = snapshot.get("resolved")
    if not resolved:
        return "none"
    return str(resolved.get("transport") or "-")


def heartbeat_summary(snapshot: dict[str, Any]) -> str:
    latest = snapshot.get("latest")
    cadence = f"every {snapshot.get('every_minutes', '-')}"
    if latest is None:
        return f"{'enabled' if snapshot.get('enabled') else 'disabled'} {cadence}m"
    status = str(latest.get("status", "unknown"))
    if latest.get("ack_suppressed"):
        status += " silent"
    next_due = snapshot.get("next_due_at") or "-"
    return f"{status} next={next_due}"


def format_mapping(values: dict[str, Any]) -> str:
    if not values:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in values.items())


def format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict):
        return ", ".join(f"{key}={format_value(item)}" for key, item in value.items())
    if isinstance(value, list):
        return ", ".join(format_value(item) for item in value)
    return str(value)


def to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    return getattr(value, "__dict__", {})
