from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..models import NormalizedInboundEvent, RunRecord, WorkflowPlan

if TYPE_CHECKING:
    from .core import Gateway


class ReplyRenderer:
    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    async def build_reply(
        self,
        run: RunRecord,
        event: NormalizedInboundEvent,
        execution_results: list[dict[str, Any]],
        workflow: WorkflowPlan,
    ) -> str:
        gw = self._gw
        deterministic_reply = self._render_workflow_reply(workflow, execution_results)
        if deterministic_reply is not None:
            return deterministic_reply
        system_prompt = gw.context_builder.system_prompt(run.session_key, run.user_id, event)
        attachment_summary = gw.context_builder.attachment_summary(event)
        attachments = gw.context_builder.attachment_list(event)
        user_message: dict[str, Any] = {"role": "user", "content": event.text}
        if attachments:
            user_message["attachments"] = attachments
        messages = [user_message]
        if attachment_summary:
            messages.append({"role": "system", "content": attachment_summary})
        if execution_results:
            messages.append({"role": "system", "content": f"Execution results: {execution_results}"})
        try:
            generated = await gw.model_router.generate(messages, system_prompt=system_prompt)
        except Exception as error:  # pragma: no cover - defensive guard around external providers
            generated = None
            execution_results.append(
                {"action": "model.generate", "success": False, "detail": f"provider_error: {error}", "artifacts": {}}
            )
        if generated:
            return generated.strip()
        memory_reads = [item for item in execution_results if item["action"] == "memory.read" and item["success"]]
        if memory_reads:
            artifacts = memory_reads[0]["artifacts"]
            lines = ["What I remember:"]
            for label, key in (
                ("User memory", "user_memory"),
                ("Session notes", "session_notes"),
                ("Session summary", "session_summary"),
                ("Recent transcript", "recent_transcript"),
                ("Global memory", "global_memory"),
            ):
                value = str(artifacts.get(key, "")).strip()
                if value:
                    lines.append(f"{label}:")
                    lines.append(value)
            return "\n".join(lines if len(lines) > 1 else ["I do not have any durable memory stored yet."])
        memory_deletes = [item for item in execution_results if item["action"] == "memory.delete"]
        if memory_deletes:
            return str(memory_deletes[0]["detail"]).strip() or "Memory updated."
        capability_reads = [item for item in execution_results if item["action"] == "capabilities.read" and item["success"]]
        if capability_reads:
            summary = str(capability_reads[0]["artifacts"].get("summary", "")).strip()
            return "What I can do:\n" + summary if summary else "I can chat, remember things, schedule reminders, and help with local tasks."
        reminder_creates = [item for item in execution_results if item["action"] == "reminder.create" and item["success"]]
        if reminder_creates:
            due_at = str(reminder_creates[0]["artifacts"].get("due_at", "")).strip()
            reminder_message = str(reminder_creates[0]["artifacts"].get("message", "")).strip()
            due_display = due_at.replace("T", " ").split("+", 1)[0] if due_at else "the requested time"
            return f"Okay. I'll message you at {due_display}: {reminder_message}"
        web_searches = [item for item in execution_results if item["action"] == "web.search" and item["success"]]
        if web_searches:
            answer = str(web_searches[-1]["artifacts"].get("answer", "")).strip()
            if answer:
                return answer
        gws_results = [item for item in execution_results if item["action"].startswith("gws.")]
        if gws_results:
            latest = gws_results[-1]
            if not latest["success"]:
                return f"Google Workspace request failed: {latest['detail']}"
            artifacts = latest.get("artifacts", {})
            output = artifacts.get("output")
            command = str(artifacts.get("command", "")).strip()
            if latest["action"] == "gws.auth.status" and isinstance(output, dict):
                lines = [
                    "Google Workspace status:",
                    f"- auth method: {output.get('auth_method', 'none')}",
                    f"- storage: {output.get('storage', 'none')}",
                    f"- credential source: {output.get('credential_source', 'none')}",
                    f"- encrypted credentials: {'yes' if output.get('encrypted_credentials_exists') else 'no'}",
                    f"- client config: {'yes' if output.get('client_config_exists') else 'no'}",
                ]
                if command:
                    lines.append(f"- command: {command}")
                return "\n".join(lines)
            if isinstance(output, dict):
                pretty = ", ".join(f"{key}={value}" for key, value in output.items())
                return f"Google Workspace result: {pretty}"
            if isinstance(output, list):
                return f"Google Workspace result: {len(output)} item(s) returned."
            if output:
                return str(output)
            return latest["detail"]
        integration_apply = [item for item in execution_results if item["action"] == "integration.apply" and item["success"]]
        if integration_apply:
            record = integration_apply[-1]["artifacts"].get("integration", {})
            integration_id = str(record.get("integration_id", "integration")).strip()
            status = str(record.get("status", "ACTIVE")).strip()
            if status == "ACTIVE":
                return "\n".join(
                    [
                        f"Integration `{integration_id}` applied.",
                        f"- status: {status}",
                        f"- required env: {', '.join(record.get('required_env', [])) or 'none'}",
                        "- startup: restored from registry + BOOT.md",
                    ]
                )
            return "\n".join(
                [
                    f"Integration `{integration_id}` applied.",
                    f"- status: {status}",
                    f"- required env: {', '.join(record.get('required_env', [])) or 'none'}",
                    f"- blocked: {record.get('last_error') or 'missing env'}",
                ]
            )
        integration_test = [item for item in execution_results if item["action"] == "integration.test"]
        if integration_test:
            record = integration_test[-1]["artifacts"].get("integration", {})
            integration_id = str(record.get("integration_id", "integration")).strip()
            return "\n".join(
                [
                    f"Integration `{integration_id}` is ready.",
                    f"- kind: {record.get('kind', 'service')}",
                    f"- staged status: {record.get('status', 'TESTED')}",
                    f"- required env: {', '.join(record.get('required_env', [])) or 'none'}",
                    f"- files: {', '.join(record.get('files', [])) or 'none'}",
                    "- next: approve to apply and activate",
                ]
            )
        if execution_results:
            parts = [f"{item['action']}: {item['detail']}" for item in execution_results]
            return "Completed requested actions. " + " | ".join(parts)
        if gw.context_builder.is_heartbeat(event):
            return "HEARTBEAT_OK"
        if attachment_summary:
            return f"I received the upload. {attachment_summary.removeprefix('Inbound attachments: ')}"
        return f"Recorded message for session {event.adapter}/{event.channel_id}: {event.text}"

    def _render_workflow_reply(self, workflow: WorkflowPlan, execution_results: list[dict[str, Any]]) -> str | None:
        if workflow.renderer.startswith("gws."):
            return self._render_gws_reply(workflow.renderer, execution_results)
        if workflow.renderer in {"memory.read", "capabilities.read", "reminder.create", "web.search"}:
            return None
        if workflow.steps:
            last = execution_results[-1] if execution_results else None
            if last and not last["success"]:
                return f"Request failed: {last['detail']}"
        return None

    def _render_gws_reply(self, renderer: str, execution_results: list[dict[str, Any]]) -> str:
        latest = execution_results[-1] if execution_results else {"success": False, "detail": "no workflow result"}
        if not latest["success"]:
            return f"Google Workspace request failed: {latest['detail']}"
        artifacts = latest.get("artifacts", {})
        output = artifacts.get("output")
        if renderer == "gws.auth.status" and isinstance(output, dict):
            lines = [
                "Google Workspace status:",
                f"- auth method: {output.get('auth_method', 'none')}",
                f"- storage: {output.get('storage', 'none')}",
                f"- credential source: {output.get('credential_source', 'none')}",
                f"- encrypted credentials: {'yes' if output.get('encrypted_credentials_exists') else 'no'}",
                f"- client config: {'yes' if output.get('client_config_exists') else 'no'}",
            ]
            return "\n".join(lines)
        if renderer == "gws.gmail.latest" and isinstance(output, dict):
            lines = ["Latest Gmail message:"]
            for label, key in (("from", "from"), ("to", "to"), ("subject", "subject"), ("date", "date")):
                value = str(output.get(key, "")).strip()
                if value:
                    lines.append(f"- {label}: {value}")
            body = str(output.get("body_preview", "") or output.get("snippet", "")).strip()
            if body:
                lines.extend(["", body])
            return "\n".join(lines)
        if renderer in {"gws.gmail.search", "gws.gmail.triage"} and isinstance(output, dict):
            messages = output.get("messages", [])
            if isinstance(messages, list) and messages:
                return "\n".join(["Gmail results:"] + [f"- {item.get('id', 'message')}" for item in messages[:5] if isinstance(item, dict)])
            return "Gmail search returned no messages."
        if renderer == "gws.gmail.send" and isinstance(output, dict):
            message_id = str(output.get("id", "")).strip()
            label_ids = output.get("labelIds")
            lines = ["Gmail sent."]
            if message_id:
                lines.append(f"- message id: {message_id}")
            if isinstance(label_ids, list) and label_ids:
                lines.append(f"- labels: {', '.join(str(item) for item in label_ids[:5])}")
            return "\n".join(lines)
        if renderer in {"gws.calendar.agenda", "gws.workflow.meeting.prep"}:
            items = []
            if isinstance(output, dict):
                for key in ("items", "events"):
                    maybe = output.get(key)
                    if isinstance(maybe, list):
                        items = maybe
                        break
            elif isinstance(output, list):
                items = output
            if not items:
                return "No calendar items found."
            lines = ["Calendar:"]
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                summary = str(item.get("summary", item.get("title", "event"))).strip() or "event"
                start = item.get("start")
                when = ""
                if isinstance(start, dict):
                    when = str(start.get("dateTime", start.get("date", ""))).strip()
                lines.append(f"- {summary}" + (f" | {when}" if when else ""))
            return "\n".join(lines)
        if renderer in {"gws.drive.search", "gws.drive.upload"}:
            files = []
            if isinstance(output, dict):
                maybe = output.get("files")
                if isinstance(maybe, list):
                    files = maybe
            if renderer == "gws.drive.upload" and isinstance(output, dict) and output.get("id"):
                name = str(output.get("name", "file")).strip()
                return f"Drive upload complete: {name} ({output.get('id')})"
            if files:
                lines = ["Drive files:"]
                for item in files[:5]:
                    if isinstance(item, dict):
                        lines.append(f"- {item.get('name', 'file')} ({item.get('id', '')})")
                return "\n".join(lines)
        if renderer == "gws.docs.create" and isinstance(output, dict):
            return f"Doc created: {output.get('name', 'document')} ({output.get('id', '')})"
        if renderer == "gws.docs.write" and isinstance(output, dict):
            doc_id = str(output.get("documentId", "")).strip()
            return f"Doc updated." + (f" Document id: {doc_id}" if doc_id else "")
        if renderer == "gws.sheets.create" and isinstance(output, dict):
            title = str(output.get("properties", {}).get("title", "sheet")).strip()
            sheet_id = str(output.get("spreadsheetId", "")).strip()
            return f"Sheet created: {title}" + (f" ({sheet_id})" if sheet_id else "")
        if renderer in {"gws.sheets.read", "gws.sheets.append"} and isinstance(output, dict):
            values = output.get("values")
            if isinstance(values, list):
                return "Sheet values:\n" + "\n".join(" | ".join(str(cell) for cell in row) for row in values[:10] if isinstance(row, list))
            updated_range = str(output.get("updates", {}).get("updatedRange", "")).strip()
            if updated_range:
                return f"Sheet updated: {updated_range}"
        if renderer == "gws.calendar.create" and isinstance(output, dict):
            lines = [f"Calendar event created: {output.get('summary', 'event')}"]
            start = output.get("start")
            end = output.get("end")
            if isinstance(start, dict):
                when = str(start.get("dateTime", start.get("date", ""))).strip()
                if when:
                    lines.append(f"- start: {when}")
                timezone = str(start.get("timeZone", "")).strip()
                if timezone:
                    lines.append(f"- timezone: {timezone}")
            if isinstance(end, dict):
                when = str(end.get("dateTime", end.get("date", ""))).strip()
                if when:
                    lines.append(f"- end: {when}")
            attendees = output.get("attendees")
            if isinstance(attendees, list) and attendees:
                emails = [str(item.get("email", "")).strip() for item in attendees if isinstance(item, dict) and str(item.get("email", "")).strip()]
                if emails:
                    lines.append(f"- attendees: {', '.join(emails)}")
            event_id = str(output.get("id", "")).strip()
            if event_id:
                lines.append(f"- event id: {event_id}")
            link = str(output.get("htmlLink", "")).strip()
            if link:
                lines.append(f"- link: {link}")
            return "\n".join(lines)
        if isinstance(output, dict):
            pretty = ", ".join(f"{key}={value}" for key, value in output.items())
            return f"Google Workspace result: {pretty}"
        if isinstance(output, list):
            return f"Google Workspace result: {len(output)} item(s) returned."
        return str(output or latest["detail"])
