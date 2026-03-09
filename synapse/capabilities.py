from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    action: str
    family: str
    description: str
    args_hint: str = ""
    prompt_visible: bool = True
    user_visible: bool = True

    def prompt_line(self) -> str:
        args = f" {self.args_hint}" if self.args_hint else ""
        return f"- {self.action}{args}: {self.description}"

    def user_line(self) -> str:
        args = f" {self.args_hint}" if self.args_hint else ""
        return f"- {self.action}{args}: {self.description}"


class CapabilityRegistry:
    def __init__(self, definitions: list[CapabilityDefinition]) -> None:
        self._definitions = definitions
        self._by_action = {item.action: item for item in definitions}

    def get(self, action: str) -> CapabilityDefinition | None:
        return self._by_action.get(action)

    def prompt_bundle(self, *, family: str | None = None, actions: list[str] | None = None) -> str:
        selected = self._definitions
        if family is not None:
            selected = [item for item in selected if item.family == family]
        if actions is not None:
            allowed = set(actions)
            selected = [item for item in selected if item.action in allowed]
        lines = [item.prompt_line() for item in selected if item.prompt_visible]
        return "\n".join(lines)

    def family_bundle(self) -> str:
        grouped: dict[str, list[str]] = {}
        for item in self._definitions:
            grouped.setdefault(item.family, []).append(item.action)
        lines = ["Runtime capability families:"]
        for family in sorted(grouped):
            actions = ", ".join(sorted(grouped[family]))
            lines.append(f"- {family}: {actions}")
        return "\n".join(lines)

    def user_bundle(self) -> str:
        lines = [item.user_line() for item in self._definitions if item.user_visible]
        return "\n".join(lines)


DEFAULT_CAPABILITY_REGISTRY = CapabilityRegistry(
    [
        CapabilityDefinition("skills.read", "skills", "Load one or more SKILL.md files on demand.", "{skill_ids:[...]}"),
        CapabilityDefinition("gws.inspect", "gws", "Inspect Google Workspace help or schema without changing data.", "{argv:[...], service?:string}"),
        CapabilityDefinition("gws.exec", "gws", "Run a Google Workspace CLI command directly when a higher-level action is not enough.", "{argv:[...], service?:string}"),
        CapabilityDefinition("gws.auth.status", "gws", "Check Google Workspace CLI auth status.", "{}"),
        CapabilityDefinition("gws.gmail.latest", "gws", "Fetch the latest Gmail message.", "{}"),
        CapabilityDefinition("gws.gmail.search", "gws", "Search Gmail messages.", "{query, limit?}"),
        CapabilityDefinition("gws.gmail.get", "gws", "Fetch a Gmail message by id.", "{message_id, format?}"),
        CapabilityDefinition("gws.gmail.triage", "gws", "Triage Gmail messages with a focused query or label set.", "{limit?, query?, labels?}"),
        CapabilityDefinition("gws.gmail.send", "gws", "Send a Gmail message.", "{to, subject, body}"),
        CapabilityDefinition("gws.calendar.agenda", "gws", "Read calendar agenda data for a time window.", "{today?, tomorrow?, week?, days?}"),
        CapabilityDefinition("gws.workflow.meeting.prep", "gws", "Prepare for the next meeting using calendar context.", "{}"),
        CapabilityDefinition("gws.calendar.event.create", "gws", "Create a Google Calendar event.", "{summary, start, end, timezone?, attendees?, description?, location?, calendar_id?}"),
        CapabilityDefinition("gws.drive.search", "gws", "Search Google Drive files.", "{query, limit?}"),
        CapabilityDefinition("gws.drive.upload", "gws", "Upload a file to Google Drive.", "{path, name?, parent_id?}"),
        CapabilityDefinition("gws.drive.text.create", "gws", "Create a simple text file in Google Drive.", "{name, text, parent_id?}"),
        CapabilityDefinition("gws.docs.create", "gws", "Create a Google Doc.", "{name}"),
        CapabilityDefinition("gws.docs.write", "gws", "Write or append text to a Google Doc.", "{document_id, text}"),
        CapabilityDefinition("gws.sheets.create", "gws", "Create a Google Sheet.", "{title}"),
        CapabilityDefinition("gws.sheets.read", "gws", "Read a range from Google Sheets.", "{spreadsheet_id, range}"),
        CapabilityDefinition("gws.sheets.append", "gws", "Append rows to Google Sheets.", "{spreadsheet_id, range, values}"),
        CapabilityDefinition("web.search", "web", "Search the web for current information.", "{query}"),
        CapabilityDefinition("web.fetch", "web", "Fetch the contents of a URL.", "{url}"),
        CapabilityDefinition("reminder.create", "reminders", "Schedule a reminder message.", "{adapter, channel_id, message, due_at}"),
        CapabilityDefinition("memory.read", "memory", "Read current user/session/global memory.", "{scope: all|user|session|global}"),
        CapabilityDefinition("memory.write", "memory", "Write user/session/global memory.", "{scope, content, name?}", user_visible=False),
        CapabilityDefinition("memory.delete", "memory", "Delete user/session/global memory entries.", "{scope, content, name?}", user_visible=False),
        CapabilityDefinition("capabilities.read", "system", "List high-level runtime capabilities.", "{}", prompt_visible=False, user_visible=False),
        CapabilityDefinition("shell.exec", "shell", "Run a shell command under runtime policy.", "{command}"),
        CapabilityDefinition("self.describe", "self", "Describe what Synapse is, its architecture, capabilities, and limitations.", "{}"),
        CapabilityDefinition("self.health", "self", "Report current health snapshot: run stats, adapter status, pending items.", "{}"),
        CapabilityDefinition("self.capabilities", "self", "List all registered capabilities and loaded skills.", "{}"),
        CapabilityDefinition("self.gaps", "self", "Identify known limitations and missing capabilities.", "{}"),
        CapabilityDefinition("diagnosis.report", "diagnosis", "Analyze recent runs, detect gaps, and suggest improvements.", "{window_hours?: int}"),
        # Finance — Read (safe)
        CapabilityDefinition("finance.holdings.read", "finance", "View equity holdings with P&L from connected broker.", "{}"),
        CapabilityDefinition("finance.positions.read", "finance", "View open positions (intraday/delivery) from broker.", "{}"),
        CapabilityDefinition("finance.margins.read", "finance", "View available trading margins.", "{}"),
        CapabilityDefinition("finance.mf.holdings", "finance", "View mutual fund holdings with current value.", "{}"),
        CapabilityDefinition("finance.mf.nav_history", "finance", "Fetch NAV history for a mutual fund scheme.", "{scheme_code}"),
        CapabilityDefinition("finance.mf.sip_xirr", "finance", "Calculate SIP XIRR for a mutual fund.", "{scheme_code, sip_amount?, start_date?}"),
        CapabilityDefinition("finance.technical.analyze", "finance", "Run technical analysis on a symbol.", "{symbol, interval?}"),
        CapabilityDefinition("finance.technical.scan", "finance", "Scan for swing trade setups matching criteria.", "{criteria?, market?}"),
        CapabilityDefinition("finance.chart.capture", "finance", "Capture a TradingView chart screenshot.", "{symbol, interval?}"),
        CapabilityDefinition("finance.chart.analyze", "finance", "Analyze a chart image for patterns using vision.", "{symbol, interval?}"),
        CapabilityDefinition("finance.sentiment.analyze", "finance", "Analyze market sentiment for a symbol or sector.", "{symbol?, sector?}"),
        CapabilityDefinition("finance.macro.summary", "finance", "Summarize macro indicators and economic calendar.", "{}"),
        CapabilityDefinition("finance.portfolio.summary", "finance", "Generate portfolio summary with allocation breakdown.", "{}"),
        CapabilityDefinition("finance.portfolio.risk", "finance", "Analyze portfolio risk metrics.", "{}"),
        # Finance — Write
        CapabilityDefinition("finance.trade.suggest", "finance", "Suggest trade ideas based on analysis (advisory only).", "{symbol?, strategy?}"),
        CapabilityDefinition("finance.trade.gtt_place", "finance", "Place a GTT order on broker (requires approval).", "{symbol, trigger_price, quantity, transaction_type, order_type}"),
    ]
)
