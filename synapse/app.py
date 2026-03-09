from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass

from pydantic import BaseModel
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .models import GatewayResult, NormalizedInboundEvent
from .runtime import Runtime, build_runtime


class InboundEventRequest(BaseModel):
    adapter: str
    channel_id: str
    user_id: str
    message_id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


def create_app(runtime: Runtime | None = None, *, root: Path | None = None) -> FastAPI:
    runtime_instance = runtime or build_runtime(root=root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime_instance.start_background_services()
        try:
            yield
        finally:
            runtime_instance.stop_background_services()

    app = FastAPI(title="Agent Runtime MVP", lifespan=lifespan)
    app.state.runtime = runtime_instance

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return runtime_instance.store.health_snapshot()

    @app.get("/api/auth")
    async def auth_health() -> dict[str, Any]:
        return runtime_instance.auth.health_view()

    @app.get("/api/gws")
    async def gws_health() -> dict[str, Any]:
        return runtime_instance.gws.status()

    @app.get("/api/memory")
    async def memory_snapshot() -> dict[str, Any]:
        return runtime_instance.memory.snapshot()

    @app.get("/api/workspace")
    async def workspace_snapshot() -> dict[str, Any]:
        return runtime_instance.workspace.snapshot()

    @app.get("/api/adapters/telegram")
    async def telegram_snapshot() -> dict[str, Any]:
        return runtime_instance.telegram.status_snapshot()

    @app.get("/api/runs")
    async def list_runs() -> list[dict[str, Any]]:
        return [serialize(item) for item in runtime_instance.store.list_runs()]

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> list[dict[str, Any]]:
        run = runtime_instance.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run id: {run_id}")
        return runtime_instance.store.list_run_events(run_id)

    @app.get("/api/logs")
    async def logs() -> list[dict[str, Any]]:
        return runtime_instance.store.list_recent_run_events()

    @app.get("/api/approvals")
    async def list_approvals() -> list[dict[str, Any]]:
        return [serialize(item) for item in runtime_instance.store.list_pending_approvals()]

    @app.get("/api/self")
    async def self_describe() -> dict[str, Any]:
        introspector = runtime_instance.introspector
        if introspector is None:
            return {"error": "introspector not configured"}
        capabilities = introspector.discover_capabilities()
        skills = introspector.discover_skills()
        plugins = introspector.discover_plugins()
        limitations = [lim.model_dump() for lim in introspector.discover_limitations()]
        architecture = [c.model_dump() for c in introspector.build_architecture().components]
        return {
            "identity": {"name": "Synapse", "version": "0.1.0"},
            "capabilities": capabilities,
            "skills": skills,
            "plugins": plugins,
            "limitations": limitations,
            "architecture": architecture,
        }

    @app.get("/api/diagnosis")
    async def diagnosis_report() -> dict[str, Any]:
        engine = runtime_instance.diagnosis_engine
        if engine is None:
            return {"error": "diagnosis engine not configured"}
        report = engine.analyze_runs()
        return report.to_dict()

    @app.get("/api/skills")
    async def list_skills() -> list[dict[str, Any]]:
        return [serialize(item) for item in runtime_instance.skills.skills.values()]

    @app.get("/api/integrations")
    async def list_integrations() -> list[dict[str, Any]]:
        return [serialize(item) for item in runtime_instance.integrations.list_integrations()]

    @app.get("/api/integrations/{integration_id}")
    async def get_integration(integration_id: str) -> dict[str, Any]:
        record = runtime_instance.integrations.get(integration_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown integration: {integration_id}")
        return serialize(record)

    @app.get("/api/heartbeat")
    async def heartbeat_status() -> dict[str, Any]:
        snapshot = runtime_instance.heartbeat_snapshot()
        snapshot["history"] = [serialize(item) for item in runtime_instance.store.list_heartbeats()]
        return snapshot

    @app.post("/api/runs/inbound")
    async def ingest_inbound(payload: InboundEventRequest) -> dict[str, Any]:
        event = NormalizedInboundEvent(
            adapter=payload.adapter,
            channel_id=payload.channel_id,
            user_id=payload.user_id,
            message_id=payload.message_id,
            text=payload.text,
            metadata=payload.metadata,
        )
        result = await runtime_instance.gateway.ingest(event)
        runtime_instance.deliver_result(result)
        return serialize(result)

    @app.post("/api/adapters/telegram/webhook")
    async def telegram_webhook(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            event = runtime_instance.telegram.normalize_update(payload)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        result = await runtime_instance.gateway.ingest(event)
        try:
            runtime_instance.deliver_result(result)
        except Exception as error:  # pragma: no cover - network failure path
            runtime_instance.store.upsert_adapter_health(
                adapter="telegram",
                status="error",
                auth_required=False,
                last_error=str(error),
            )
        return serialize(result)

    @app.post("/api/approvals/{approval_id}/approve")
    async def approve(approval_id: str) -> dict[str, Any]:
        try:
            result: GatewayResult = await runtime_instance.gateway.approve(approval_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        runtime_instance.deliver_result(result)
        return serialize(result)

    @app.get("/console", response_class=HTMLResponse)
    async def console_overview() -> str:
        health_snapshot = runtime_instance.store.health_snapshot()
        auth_snapshot = runtime_instance.auth.health_view()
        body = "\n".join(
            [
                nav_html(),
                metric_grid(
                    [
                        ("Pending approvals", str(health_snapshot["pending_approvals"])),
                        ("Queued events", str(health_snapshot["queued_events"])),
                        ("Resolved provider", resolved_provider_label(auth_snapshot)),
                        ("Loaded skills", str(len(runtime_instance.skills.skills))),
                        ("Playbooks", str(len(runtime_instance.workspace.snapshot()["playbooks"]))),
                        ("Heartbeat", heartbeat_label(runtime_instance.heartbeat_snapshot())),
                        ("Integrations", str(len(runtime_instance.integrations.list_integrations()))),
                        ("GWS", "enabled" if runtime_instance.gws.status()["enabled"] else "disabled"),
                    ]
                ),
                section_html("Runs by State", definition_list(health_snapshot["runs_by_state"])),
                section_html("Heartbeat", table_html(runtime_instance.store.list_heartbeats(limit=5))),
                section_html("Integrations", table_html(runtime_instance.integrations.list_integrations())),
                section_html("Adapter Health", table_html(runtime_instance.store.list_adapter_health())),
            ]
        )
        return page_html("Overview", body)

    @app.get("/console/runs", response_class=HTMLResponse)
    async def console_runs() -> str:
        runs = []
        for item in runtime_instance.store.list_runs():
            row = serialize(item)
            row["inspect_path"] = f"/console/runs/{row['run_id']}"
            runs.append(row)
        return page_html("Runs", nav_html() + section_html("Recent Runs", table_html(runs)))

    @app.get("/console/runs/{run_id}", response_class=HTMLResponse)
    async def console_run_detail(run_id: str) -> str:
        run = runtime_instance.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run id: {run_id}")
        events = runtime_instance.store.list_run_events(run_id)
        body = "\n".join(
            [
                nav_html(),
                section_html("Run", table_html([serialize(run)])),
                section_html("Events", event_timeline_html(events)),
            ]
        )
        return page_html(f"Run {run_id[:8]}", body)

    @app.get("/console/approvals", response_class=HTMLResponse)
    async def console_approvals() -> str:
        approvals = serialize(runtime_instance.store.list_pending_approvals())
        body = nav_html() + section_html("Pending Approvals", table_html(approvals) if approvals else "<p>No pending approvals.</p>")
        return page_html("Approvals", body)

    @app.get("/console/auth", response_class=HTMLResponse)
    async def console_auth() -> str:
        snapshot = runtime_instance.auth.health_view()
        sources = snapshot["sources"]
        sections = [
            nav_html(),
            metric_grid(
                [
                    ("Resolved provider", resolved_provider_label(snapshot)),
                    ("Resolution source", resolved_source_label(snapshot)),
                    ("Local profiles", str(sources["local_profiles"]["count"])),
                    ("Codex auth", "yes" if sources["codex_cli"]["available"] else "no"),
                ]
            ),
            section_html("Auth Sources", table_html([{"source": name, **details} for name, details in sources.items()])),
        ]
        return page_html("Auth", "\n".join(sections))

    @app.get("/console/gws", response_class=HTMLResponse)
    async def console_gws() -> str:
        snapshot = runtime_instance.gws.status()
        body = "\n".join(
            [
                nav_html(),
                metric_grid(
                    [
                        ("Enabled", "yes" if snapshot["enabled"] else "no"),
                        ("Installed", "yes" if snapshot["installed"] else "no"),
                        ("Auth available", "yes" if snapshot["auth_available"] else "no"),
                        ("Credential source", str(snapshot.get("credential_source", "none"))),
                    ]
                ),
                section_html("Workspace Status", definition_list(snapshot)),
            ]
        )
        return page_html("GWS", body)

    @app.get("/console/memory", response_class=HTMLResponse)
    async def console_memory() -> str:
        snapshot = runtime_instance.memory.snapshot()
        body = "\n".join(
            [
                nav_html(),
                section_html("Global Memory", table_html(snapshot["global_files"])),
                section_html("User Memory", table_html(snapshot["user_files"])),
                section_html("Session Memory", table_html(snapshot["session_files"])),
            ]
        )
        return page_html("Memory", body)

    @app.get("/console/workspace", response_class=HTMLResponse)
    async def console_workspace() -> str:
        snapshot = runtime_instance.workspace.snapshot()
        body = "\n".join(
            [
                nav_html(),
                section_html("Workspace Files", table_html(snapshot["files"])),
                section_html("Playbooks", table_html(snapshot["playbooks"])),
            ]
        )
        return page_html("Workspace", body)

    @app.get("/console/skills", response_class=HTMLResponse)
    async def console_skills() -> str:
        skills = serialize(list(runtime_instance.skills.skills.values()))
        return page_html("Skills", nav_html() + section_html("Loaded Skills", table_html(skills)))

    @app.get("/console/integrations", response_class=HTMLResponse)
    async def console_integrations() -> str:
        integrations = serialize(runtime_instance.integrations.list_integrations())
        boot_tasks = runtime_instance.integrations.boot_tasks()
        body = "\n".join(
            [
                nav_html(),
                section_html("Registry", table_html(integrations) if integrations else "<p>No integrations yet.</p>"),
                section_html("BOOT.md Tasks", table_html([{"task": task} for task in boot_tasks]) if boot_tasks else "<p>No BOOT tasks.</p>"),
            ]
        )
        return page_html("Integrations", body)

    @app.get("/console/adapters", response_class=HTMLResponse)
    async def console_adapters() -> str:
        adapters = runtime_instance.store.list_adapter_health()
        return page_html("Adapter Health", nav_html() + section_html("Adapters", table_html(adapters)))

    @app.get("/console/heartbeat", response_class=HTMLResponse)
    async def console_heartbeat() -> str:
        snapshot = runtime_instance.heartbeat_snapshot()
        history = [serialize(item) for item in runtime_instance.store.list_heartbeats()]
        body = "\n".join(
            [
                nav_html(),
                metric_grid(
                    [
                        ("Status", str(snapshot.get("status", "unknown"))),
                        ("Cadence", f"{snapshot['every_minutes']} min"),
                        ("Target", str(snapshot["target"])),
                        ("Next due", "disabled" if not snapshot["enabled"] else str(snapshot["next_due_at"] or "-")),
                    ]
                ),
                section_html("Latest Heartbeat", table_html([snapshot["latest"]]) if snapshot["latest"] else "<p>No heartbeat yet.</p>"),
                section_html("Recent Heartbeats", table_html(history) if history else "<p>No heartbeat history.</p>"),
            ]
        )
        return page_html("Heartbeat", body)

    @app.get("/console/logs", response_class=HTMLResponse)
    async def console_logs() -> str:
        events = runtime_instance.store.list_recent_run_events()
        body = "\n".join(
            [
                nav_html(),
                section_html("Recent Event Trace", event_timeline_html(events)),
            ]
        )
        return page_html("Logs", body)

    @app.get("/console/self", response_class=HTMLResponse)
    async def console_self() -> str:
        introspector = runtime_instance.introspector
        sections = [nav_html()]
        if introspector is None:
            sections.append(section_html("Self", "<p>Introspector not configured.</p>"))
        else:
            caps = introspector.discover_capabilities()
            skills_list = introspector.discover_skills()
            limitations = [lim.model_dump() for lim in introspector.discover_limitations()]
            arch = [c.model_dump() for c in introspector.build_architecture().components]
            sections.extend([
                metric_grid([
                    ("Name", "Synapse"),
                    ("Version", "0.1.0"),
                    ("Capabilities", str(len(caps))),
                    ("Skills", str(len(skills_list))),
                    ("Limitations", str(len(limitations))),
                ]),
                section_html("Architecture", table_html(arch)),
                section_html("Limitations", table_html(limitations) if limitations else "<p>None detected.</p>"),
            ])
        return page_html("Self", "\n".join(sections))

    @app.get("/console/diagnosis", response_class=HTMLResponse)
    async def console_diagnosis() -> str:
        engine = runtime_instance.diagnosis_engine
        sections = [nav_html()]
        if engine is None:
            sections.append(section_html("Diagnosis", "<p>Diagnosis engine not configured.</p>"))
        else:
            report = engine.analyze_runs()
            data = report.to_dict()
            sections.extend([
                metric_grid([
                    ("Total Runs", str(data["total_runs"])),
                    ("Completed", str(data["completed_runs"])),
                    ("Failed", str(data["failed_runs"])),
                    ("Health Score", f"{data['health_score']:.0%}"),
                ]),
                section_html("Run States", definition_list(data["run_states"]) if data["run_states"] else "<p>No runs yet.</p>"),
                section_html("Detected Gaps", table_html(data["gaps"]) if data["gaps"] else "<p>No gaps detected.</p>"),
                section_html("Suggested Improvements", table_html(data["improvements"]) if data["improvements"] else "<p>No suggestions.</p>"),
            ])
        return page_html("Diagnosis", "\n".join(sections))

    return app


def serialize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    return value


def page_html(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} | Agent Runtime</title>
  <style>
    :root {{
      --bg: #f5f1e8;
      --ink: #1f1c18;
      --card: #fffdf8;
      --line: #d2c8b6;
      --accent: #a94f2d;
    }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #f0d7b2 0, transparent 24rem),
        linear-gradient(180deg, #f8f4ed 0%, var(--bg) 100%);
    }}
    main {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1 {{
      margin: 0 0 18px;
      font-size: 2.3rem;
    }}
    nav {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 22px;
    }}
    nav a {{
      color: var(--ink);
      text-decoration: none;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.7);
      padding: 8px 12px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .metric, section {{
      background: var(--card);
      border: 1px solid var(--line);
      padding: 14px;
      box-shadow: 0 8px 24px rgba(31, 28, 24, 0.05);
    }}
    .metric strong {{
      display: block;
      font-size: 1.5rem;
      margin-top: 6px;
      color: var(--accent);
    }}
    section {{
      margin-top: 14px;
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.95rem;
    }}
    th, td {{
      text-align: left;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    code {{
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 0.9em;
    }}
    dl {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin: 0;
    }}
    dt {{
      font-weight: 700;
    }}
    dd {{
      margin: 0;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{escape(title)}</h1>
    {body}
  </main>
</body>
</html>"""


def nav_html() -> str:
    return """<nav>
<a href="/console">Overview</a>
<a href="/console/runs">Runs</a>
<a href="/console/approvals">Approvals</a>
<a href="/console/auth">Auth</a>
<a href="/console/gws">GWS</a>
<a href="/console/memory">Memory</a>
<a href="/console/workspace">Workspace</a>
<a href="/console/skills">Skills</a>
<a href="/console/integrations">Integrations</a>
<a href="/console/adapters">Adapter Health</a>
<a href="/console/heartbeat">Heartbeat</a>
<a href="/console/logs">Logs</a>
<a href="/console/self">Self</a>
<a href="/console/diagnosis">Diagnosis</a>
</nav>"""


def metric_grid(metrics: list[tuple[str, str]]) -> str:
    cards = "".join(
        f'<div class="metric"><span>{escape(label)}</span><strong>{escape(value)}</strong></div>'
        for label, value in metrics
    )
    return f'<div class="metrics">{cards}</div>'


def section_html(title: str, content: str) -> str:
    return f"<section><h2>{escape(title)}</h2>{content}</section>"


def event_timeline_html(events: list[dict[str, Any]]) -> str:
    if not events:
        return "<p>No events yet.</p>"
    items = []
    for event in events:
        payload = json_block(event.get("payload"))
        items.append(
            "\n".join(
                [
                    "<article style=\"border-top:1px solid var(--line); padding:12px 0;\">",
                    f"<div><strong>{escape(str(event.get('event_type', 'event')))}</strong> <span style=\"color:#7b6c58\">{escape(str(event.get('created_at', '')))}</span></div>",
                    f"<div style=\"margin:6px 0 8px; color:#7b6c58;\">run={escape(str(event.get('run_id', '')))} session={escape(str(event.get('session_key', '')))}</div>",
                    f"<pre style=\"white-space:pre-wrap; background:#f8f4ed; padding:10px; border:1px solid var(--line);\">{escape(payload)}</pre>",
                    "</article>",
                ]
            )
        )
    return "".join(items)


def table_html(rows: Any) -> str:
    if not rows:
        return "<p>No data.</p>"
    normalized_rows = serialize(rows)
    headers = list(normalized_rows[0].keys())
    head = "".join(f"<th>{escape(str(header))}</th>" for header in headers)
    body_rows = []
    for row in normalized_rows:
        cells = "".join(f"<td>{escape(format_cell(row.get(header)))}</td>" for header in headers)
        body_rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def definition_list(items: dict[str, Any]) -> str:
    if not items:
        return "<p>No data.</p>"
    entries = "".join(f"<div><dt>{escape(str(key))}</dt><dd>{escape(format_cell(value))}</dd></div>" for key, value in items.items())
    return f"<dl>{entries}</dl>"


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return ", ".join(f"{key}={format_cell(item)}" for key, item in value.items())
    if isinstance(value, list):
        return ", ".join(format_cell(item) for item in value)
    return str(value)


def json_block(value: Any) -> str:
    try:
        return json_dumps_pretty(value)
    except Exception:
        return format_cell(value)


def json_dumps_pretty(value: Any) -> str:
    import json

    return json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True)


def resolved_provider_label(snapshot: dict[str, Any]) -> str:
    resolved = snapshot.get("resolved")
    if not resolved:
        return "unresolved"
    return f"{resolved['provider']} / {resolved['model']}"


def resolved_source_label(snapshot: dict[str, Any]) -> str:
    resolved = snapshot.get("resolved")
    if not resolved:
        return "none"
    return str(resolved["source"])


def heartbeat_label(snapshot: dict[str, Any]) -> str:
    if not snapshot.get("enabled", False):
        return "disabled"
    if snapshot.get("status") == "configured":
        return "configured"
    latest = snapshot.get("latest")
    if latest is None:
        return "idle"
    status = latest.get("status", "unknown")
    if latest.get("ack_suppressed"):
        return f"{status} (silent)"
    return str(status)
