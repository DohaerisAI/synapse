from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable


CORE_GWS_SERVICES = ("gmail", "calendar", "drive", "docs", "sheets")
DEFAULT_GWS_ALLOWED_SERVICES = ",".join(CORE_GWS_SERVICES)


@dataclass(slots=True)
class GWSOperation:
    action: str
    service: str
    command: list[str]
    interactive: bool = False
    description: str = ""

    @property
    def preview(self) -> str:
        return shlex.join(self.command)


class GWSBridge:
    def __init__(
        self,
        *,
        enabled: bool,
        binary: str = "gws",
        allowed_services: set[str] | None = None,
        env: dict[str, str] | None = None,
        home: Path | None = None,
        workdir: str = ".",
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.enabled = enabled
        self.binary = binary.strip() or "gws"
        self.allowed_services = allowed_services or set(CORE_GWS_SERVICES)
        self.env = env if env is not None else dict(os.environ)
        self.home = home if home is not None else Path.home()
        self.workdir = workdir
        self.runner = runner

    def status(self) -> dict[str, Any]:
        config_dir = self._config_dir()
        binary_path = self._resolve_binary()
        env_credentials = self.env.get("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", "").strip()
        token_env = bool(self.env.get("GOOGLE_WORKSPACE_CLI_TOKEN", "").strip())
        snapshot: dict[str, Any] = {
            "enabled": self.enabled,
            "binary": self.binary,
            "binary_path": None if binary_path is None else str(binary_path),
            "installed": binary_path is not None,
            "allowed_services": sorted(self.allowed_services),
            "config_dir": str(config_dir),
            "credential_env_configured": bool(env_credentials or token_env),
            "credentials_file": env_credentials or "",
            "token_env_configured": token_env,
            "encrypted_credentials": str(config_dir / "credentials.enc"),
            "encrypted_credentials_exists": (config_dir / "credentials.enc").exists(),
            "plain_credentials": str(config_dir / "credentials.json"),
            "plain_credentials_exists": (config_dir / "credentials.json").exists(),
            "client_config": str(config_dir / "client_secret.json"),
            "client_config_exists": (config_dir / "client_secret.json").exists(),
            "auth_available": False,
            "credential_source": "none",
        }
        if snapshot["token_env_configured"]:
            snapshot["auth_available"] = True
            snapshot["credential_source"] = "token_env_var"
        elif env_credentials:
            snapshot["auth_available"] = True
            snapshot["credential_source"] = "credentials_file_env"
        elif snapshot["encrypted_credentials_exists"]:
            snapshot["auth_available"] = True
            snapshot["credential_source"] = "encrypted_credentials"
        elif snapshot["plain_credentials_exists"]:
            snapshot["auth_available"] = True
            snapshot["credential_source"] = "plain_credentials"

        if binary_path is None:
            snapshot["last_error"] = "gws binary not found"
            return snapshot
        if not self.enabled:
            return snapshot
        if snapshot["auth_available"]:
            snapshot["status_mode"] = "local_credentials"
            return snapshot

        try:
            completed = self._run_command_sync([self.binary, "auth", "status"], timeout=5)
        except Exception as error:
            snapshot["last_error"] = str(error)
            snapshot["status_mode"] = "local_fallback"
            return snapshot
        if completed.returncode != 0:
            snapshot["last_error"] = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            snapshot["status_mode"] = "local_fallback"
            return snapshot
        parsed = self._parse_json_output(completed.stdout)
        if isinstance(parsed, dict):
            snapshot["auth_status"] = parsed
            auth_method = str(parsed.get("auth_method", "none")).strip().lower()
            snapshot["auth_available"] = auth_method != "none" or snapshot["auth_available"]
            snapshot["credential_source"] = str(parsed.get("credential_source", snapshot["credential_source"]))
            snapshot["status_mode"] = "live"
        else:
            snapshot["status_mode"] = "local_fallback"
        return snapshot

    def preview_action(self, action: str, payload: dict[str, Any]) -> str:
        if action == "gws.gmail.latest":
            params = json.dumps({"userId": "me", "maxResults": 1}, ensure_ascii=True)
            return f"{shlex.join([self.binary, 'gmail', 'users', 'messages', 'list', '--params', params])} -> fetch latest message details"
        if action == "gws.drive.text.create":
            name = str(payload.get("name", "Untitled.txt")).strip() or "Untitled.txt"
            return f"{self.binary} drive files create --json {json.dumps({'name': name}, ensure_ascii=True)} --upload <tempfile>"
        return self.build_operation(action, payload).preview

    async def execute(self, action: str, payload: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        if not self.enabled:
            return False, "Google Workspace integration is disabled.", {}
        if action == "gws.auth.status":
            snapshot = self.status()
            source = snapshot.get("credential_source", "none")
            mode = snapshot.get("status_mode", "unknown")
            return True, f"gws auth status loaded (source={source}, mode={mode})", {"service": "auth", "output": snapshot}
        if action == "gws.gmail.latest":
            return await self._execute_gmail_latest()
        try:
            operation = self.build_operation(action, payload)
        except Exception as error:
            return False, str(error), {}
        if operation.service != "auth" and operation.service not in self.allowed_services:
            return False, f"Google Workspace service is not allowed: {operation.service}", {"command": operation.preview}
        completed = await self._run_command(operation.command, timeout=600 if operation.interactive else 120)
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        parsed = self._parse_json_output(stdout)
        if action == "gws.gmail.get" and isinstance(parsed, dict):
            parsed = self._normalize_gmail_message(parsed)
        artifacts = {
            "command": operation.preview,
            "service": operation.service,
            "description": operation.description,
            "interactive": operation.interactive,
            "output": parsed if parsed is not None else stdout,
        }
        if stderr:
            artifacts["stderr"] = stderr
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit code {completed.returncode}"
            return False, detail, artifacts
        detail = operation.description or f"executed {action}"
        if action == "gws.auth.status" and isinstance(parsed, dict):
            source = parsed.get("credential_source") or "unknown"
            method = parsed.get("auth_method") or "none"
            detail = f"gws auth status loaded ({method}, source={source})"
        return True, detail, artifacts

    def build_operation(self, action: str, payload: dict[str, Any]) -> GWSOperation:
        command: list[str]
        if action == "gws.auth.status":
            return GWSOperation(action, "auth", [self.binary, "auth", "status"], description="Google Workspace auth status")
        if action == "gws.auth.setup":
            return GWSOperation(action, "auth", [self.binary, "auth", "setup"], interactive=True, description="Google Workspace auth setup")
        if action == "gws.auth.login":
            scopes = str(payload.get("scopes") or self.default_scopes()).strip()
            return GWSOperation(
                action,
                "auth",
                [self.binary, "auth", "login", "-s", scopes],
                interactive=True,
                description="Google Workspace auth login",
            )
        if action == "gws.gmail.search":
            params = {"userId": "me", "maxResults": int(payload.get("limit", 10))}
            query = str(payload.get("query", "")).strip()
            if query:
                params["q"] = query
            command = [self.binary, "gmail", "users", "messages", "list", "--params", json.dumps(params, ensure_ascii=True)]
            return GWSOperation(action, "gmail", command, description="Search Gmail messages")
        if action == "gws.gmail.get":
            message_id = str(payload.get("message_id", "")).strip()
            if not message_id:
                raise ValueError("gmail get requires message_id")
            params = {"userId": "me", "id": message_id, "format": str(payload.get("format", "full")).strip() or "full"}
            command = [self.binary, "gmail", "users", "messages", "get", "--params", json.dumps(params, ensure_ascii=True)]
            return GWSOperation(action, "gmail", command, description="Fetch Gmail message details")
        if action == "gws.gmail.triage":
            command = [self.binary, "gmail", "+triage", "--format", "json", "--max", str(int(payload.get("limit", 10)))]
            query = str(payload.get("query", "")).strip()
            if query:
                command.extend(["--query", query])
            if bool(payload.get("labels")):
                command.append("--labels")
            return GWSOperation(action, "gmail", command, description="Triage Gmail inbox")
        if action == "gws.gmail.send":
            to = str(payload.get("to", "")).strip()
            subject = str(payload.get("subject", "")).strip()
            body = str(payload.get("body", "")).strip()
            if not all((to, subject, body)):
                raise ValueError("gmail send requires to, subject, and body")
            raw_message = self._gmail_raw_message(to=to, subject=subject, body=body)
            command = [
                self.binary,
                "gmail",
                "users",
                "messages",
                "send",
                "--params",
                json.dumps({"userId": "me"}, ensure_ascii=True),
                "--json",
                json.dumps({"raw": raw_message}, ensure_ascii=True),
            ]
            return GWSOperation(action, "gmail", command, description="Send a Gmail message")
        if action == "gws.calendar.agenda":
            command = [self.binary, "calendar", "+agenda", "--format", "json"]
            if bool(payload.get("today")):
                command.append("--today")
            elif bool(payload.get("tomorrow")):
                command.append("--tomorrow")
            elif bool(payload.get("week")):
                command.append("--week")
            elif payload.get("days") is not None:
                command.extend(["--days", str(max(1, int(payload.get("days", 1))))])
            return GWSOperation(action, "calendar", command, description="Show Google Calendar agenda")
        if action == "gws.workflow.meeting.prep":
            command = [self.binary, "workflow", "+meeting-prep", "--format", "json"]
            calendar = str(payload.get("calendar", "")).strip()
            if calendar:
                command.extend(["--calendar", calendar])
            return GWSOperation(action, "calendar", command, description="Prepare for the next meeting")
        if action == "gws.calendar.event.create":
            summary = str(payload.get("summary", "")).strip()
            start = str(payload.get("start", "")).strip()
            end = str(payload.get("end", "")).strip()
            if not all((summary, start, end)):
                raise ValueError("calendar create requires summary, start, and end")
            self._validate_rfc3339_datetime(start, field="start")
            self._validate_rfc3339_datetime(end, field="end")
            attendees = [item.strip() for item in payload.get("attendees", []) if isinstance(item, str) and item.strip()]
            timezone = str(payload.get("timezone", "")).strip()
            params = {"calendarId": str(payload.get("calendar_id", "primary")).strip() or "primary"}
            if attendees:
                params["sendUpdates"] = str(payload.get("send_updates", "all")).strip() or "all"
            body: dict[str, Any] = {
                "summary": summary,
                "start": {"dateTime": start},
                "end": {"dateTime": end},
            }
            if timezone:
                body["start"]["timeZone"] = timezone
                body["end"]["timeZone"] = timezone
            description = str(payload.get("description", "")).strip()
            if description:
                body["description"] = description
            location = str(payload.get("location", "")).strip()
            if location:
                body["location"] = location
            if attendees:
                body["attendees"] = [{"email": item} for item in attendees]
            command = [
                self.binary,
                "calendar",
                "events",
                "insert",
                "--params",
                json.dumps(params, ensure_ascii=True),
                "--json",
                json.dumps(body, ensure_ascii=True),
            ]
            return GWSOperation(action, "calendar", command, description="Create a Google Calendar event")
        if action == "gws.drive.search":
            query = str(payload.get("query", "")).strip()
            escaped = query.replace("'", "\\'")
            params = {
                "q": f"name contains '{escaped}' or fullText contains '{escaped}'",
                "pageSize": int(payload.get("limit", 10)),
                "fields": "files(id,name,mimeType,webViewLink)",
            }
            command = [self.binary, "drive", "files", "list", "--params", json.dumps(params, ensure_ascii=True)]
            return GWSOperation(action, "drive", command, description="Search Google Drive files")
        if action == "gws.drive.upload":
            upload_path = Path(str(payload.get("path", "")).strip()).expanduser()
            if not upload_path.exists():
                raise ValueError(f"upload path does not exist: {upload_path}")
            body: dict[str, Any] = {"name": str(payload.get("name", upload_path.name)).strip() or upload_path.name}
            parent_id = str(payload.get("parent_id", "")).strip()
            if parent_id:
                body["parents"] = [parent_id]
            command = [
                self.binary,
                "drive",
                "files",
                "create",
                "--json",
                json.dumps(body, ensure_ascii=True),
                "--upload",
                str(upload_path),
            ]
            return GWSOperation(action, "drive", command, description="Upload a file to Google Drive")
        if action == "gws.drive.text.create":
            name = str(payload.get("name", "")).strip() or "Untitled.txt"
            if not name.lower().endswith(".txt"):
                name = name + ".txt"
            fd, temp_path = tempfile.mkstemp(prefix="gws-drive-", suffix=".txt")
            os.close(fd)
            temp_file = Path(temp_path)
            temp_file.write_text(str(payload.get("text", "")), encoding="utf-8")
            body: dict[str, Any] = {"name": name}
            parent_id = str(payload.get("parent_id", "")).strip()
            if parent_id:
                body["parents"] = [parent_id]
            command = [
                self.binary,
                "drive",
                "files",
                "create",
                "--json",
                json.dumps(body, ensure_ascii=True),
                "--upload",
                str(temp_file),
            ]
            return GWSOperation(action, "drive", command, description="Create a text file in Google Drive")
        if action == "gws.docs.create":
            name = str(payload.get("name", "")).strip() or "Untitled document"
            command = [
                self.binary,
                "drive",
                "files",
                "create",
                "--json",
                json.dumps({"name": name, "mimeType": "application/vnd.google-apps.document"}, ensure_ascii=True),
            ]
            return GWSOperation(action, "docs", command, description="Create a Google Doc")
        if action == "gws.docs.write":
            document_id = str(payload.get("document_id", "")).strip()
            text = str(payload.get("text", "")).strip()
            if not all((document_id, text)):
                raise ValueError("docs write requires document_id and text")
            params = {"documentId": document_id}
            body = {"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text + "\n"}}]}
            command = [
                self.binary,
                "docs",
                "documents",
                "batchUpdate",
                "--params",
                json.dumps(params, ensure_ascii=True),
                "--json",
                json.dumps(body, ensure_ascii=True),
            ]
            return GWSOperation(action, "docs", command, description="Append text to a Google Doc")
        if action == "gws.sheets.create":
            title = str(payload.get("title", "")).strip() or "Untitled sheet"
            body = {"properties": {"title": title}}
            command = [self.binary, "sheets", "spreadsheets", "create", "--json", json.dumps(body, ensure_ascii=True)]
            return GWSOperation(action, "sheets", command, description="Create a Google Sheet")
        if action == "gws.sheets.read":
            spreadsheet_id = str(payload.get("spreadsheet_id", "")).strip()
            value_range = str(payload.get("range", "")).strip()
            if not all((spreadsheet_id, value_range)):
                raise ValueError("sheets read requires spreadsheet_id and range")
            params = {"spreadsheetId": spreadsheet_id, "range": value_range}
            command = [self.binary, "sheets", "spreadsheets", "values", "get", "--params", json.dumps(params, ensure_ascii=True)]
            return GWSOperation(action, "sheets", command, description="Read values from Google Sheets")
        if action == "gws.sheets.append":
            spreadsheet_id = str(payload.get("spreadsheet_id", "")).strip()
            value_range = str(payload.get("range", "")).strip()
            values = payload.get("values")
            if not all((spreadsheet_id, value_range)) or values is None:
                raise ValueError("sheets append requires spreadsheet_id, range, and values")
            params = {"spreadsheetId": spreadsheet_id, "range": value_range, "valueInputOption": "USER_ENTERED"}
            body = {"values": values}
            command = [
                self.binary,
                "sheets",
                "spreadsheets",
                "values",
                "append",
                "--params",
                json.dumps(params, ensure_ascii=True),
                "--json",
                json.dumps(body, ensure_ascii=True),
            ]
            return GWSOperation(action, "sheets", command, description="Append values to Google Sheets")
        if action == "gws.exec":
            argv = payload.get("argv")
            if not isinstance(argv, list) or not argv:
                raise ValueError("gws exec requires argv")
            command = [self.binary, *[str(item) for item in argv]]
            service = str(payload.get("service", "generic")).strip() or "generic"
            return GWSOperation(action, service, command, description="Execute generic gws command")
        if action == "gws.inspect":
            argv = payload.get("argv")
            if not isinstance(argv, list) or not argv:
                raise ValueError("gws inspect requires argv")
            parts = [str(item).strip() for item in argv if str(item).strip()]
            lowered = [item.lower() for item in parts]
            if not parts or not (parts[0] == "schema" or "--help" in lowered or "-h" in lowered):
                raise ValueError("gws inspect only allows schema/help commands")
            service = "schema" if parts[0] == "schema" else str(payload.get("service", parts[0])).strip() or "generic"
            command = [self.binary, *parts]
            return GWSOperation(action, service, command, description="Inspect gws help or schema")
        raise ValueError(f"unsupported gws action: {action}")

    def default_scopes(self) -> str:
        return ",".join(sorted(self.allowed_services))

    def _run_command_sync(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(self.env)
        if self.runner is not None:
            return self.runner(command, env=env, cwd=self.workdir, timeout=timeout)
        return subprocess.run(command, capture_output=True, text=True, cwd=self.workdir, env=env, timeout=timeout)

    async def _run_command(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(self.env)
        if self.runner is not None:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self.runner(command, env=env, cwd=self.workdir, timeout=timeout),
            )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workdir,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise
        return subprocess.CompletedProcess(
            args=command,
            returncode=process.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )

    def _resolve_binary(self) -> Path | None:
        candidate = Path(self.binary).expanduser()
        if candidate.is_file():
            return candidate
        resolved = shutil.which(self.binary)
        return None if resolved is None else Path(resolved)

    def _config_dir(self) -> Path:
        override = self.env.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "").strip()
        if override:
            return Path(override).expanduser()
        return self.home / ".config" / "gws"

    def _parse_json_output(self, output: str) -> Any:
        text = output.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _gmail_raw_message(self, *, to: str, subject: str, body: str) -> str:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        raw_bytes = message.as_bytes()
        return base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")

    def _validate_rfc3339_datetime(self, value: str, *, field: str) -> None:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise ValueError(
                f"calendar create requires RFC3339 {field} datetime, got {value!r}. "
                "Use an ISO timestamp like 2026-03-07T17:00:00+05:30."
            ) from error
        if parsed.tzinfo is None:
            raise ValueError(
                f"calendar create requires timezone-aware {field} datetime, got {value!r}. "
                "Include an offset like +05:30 or use Z."
            )

    async def _execute_gmail_latest(self) -> tuple[bool, str, dict[str, Any]]:
        list_params = {"userId": "me", "maxResults": 1}
        list_command = [self.binary, "gmail", "users", "messages", "list", "--params", json.dumps(list_params, ensure_ascii=True)]
        listed = await self._run_command(list_command, timeout=120)
        list_stdout = listed.stdout.strip()
        list_stderr = listed.stderr.strip()
        listed_parsed = self._parse_json_output(list_stdout)
        artifacts: dict[str, Any] = {
            "command": f"{shlex.join(list_command)} -> fetch latest message details",
            "service": "gmail",
            "description": "Fetch the latest Gmail message details",
            "interactive": False,
            "listing": listed_parsed if listed_parsed is not None else list_stdout,
        }
        if list_stderr:
            artifacts["stderr"] = list_stderr
        if listed.returncode != 0:
            detail = list_stderr or list_stdout or f"exit code {listed.returncode}"
            return False, detail, artifacts
        if not isinstance(listed_parsed, dict):
            return False, "gmail list returned unexpected output", artifacts
        messages = listed_parsed.get("messages")
        if not isinstance(messages, list) or not messages:
            artifacts["output"] = {"messages": [], "count": 0}
            return True, "No Gmail messages found.", artifacts
        first = messages[0] if isinstance(messages[0], dict) else {}
        message_id = str(first.get("id", "")).strip()
        if not message_id:
            return False, "gmail list did not include a message id", artifacts
        get_params = {"userId": "me", "id": message_id, "format": "full"}
        get_command = [self.binary, "gmail", "users", "messages", "get", "--params", json.dumps(get_params, ensure_ascii=True)]
        fetched = await self._run_command(get_command, timeout=120)
        get_stdout = fetched.stdout.strip()
        get_stderr = fetched.stderr.strip()
        parsed_message = self._parse_json_output(get_stdout)
        if get_stderr:
            artifacts["stderr"] = "\n".join(part for part in [artifacts.get("stderr", ""), get_stderr] if part).strip()
        if fetched.returncode != 0:
            artifacts["message_id"] = message_id
            artifacts["get_command"] = shlex.join(get_command)
            detail = get_stderr or get_stdout or f"exit code {fetched.returncode}"
            return False, detail, artifacts
        normalized = self._normalize_gmail_message(parsed_message if isinstance(parsed_message, dict) else {})
        artifacts["message_id"] = message_id
        artifacts["get_command"] = shlex.join(get_command)
        artifacts["output"] = normalized
        return True, "Fetched latest Gmail message details", artifacts

    def _normalize_gmail_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = self._gmail_headers(payload.get("payload"))
        snippet = str(payload.get("snippet", "")).strip()
        body_text = self._gmail_body_text(payload.get("payload"))
        return {
            "id": str(payload.get("id", "")).strip(),
            "thread_id": str(payload.get("threadId", "")).strip(),
            "label_ids": payload.get("labelIds", []),
            "subject": headers.get("subject", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "snippet": snippet,
            "body_text": body_text,
            "body_preview": body_text[:1000].strip(),
        }

    def _gmail_headers(self, payload: Any) -> dict[str, str]:
        if not isinstance(payload, dict):
            return {}
        headers = payload.get("headers")
        if not isinstance(headers, list):
            return {}
        extracted: dict[str, str] = {}
        for header in headers:
            if not isinstance(header, dict):
                continue
            name = str(header.get("name", "")).strip().lower()
            value = str(header.get("value", "")).strip()
            if name in {"subject", "from", "to", "date"} and value:
                extracted[name] = value
        return extracted

    def _gmail_body_text(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        mime_type = str(payload.get("mimeType", "")).strip().lower()
        body = payload.get("body")
        if mime_type.startswith("text/plain"):
            decoded = self._decode_gmail_body(body)
            if decoded:
                return decoded
        parts = payload.get("parts")
        if isinstance(parts, list):
            for part in parts:
                text = self._gmail_body_text(part)
                if text:
                    return text
        decoded = self._decode_gmail_body(body)
        return decoded or ""

    def _decode_gmail_body(self, body: Any) -> str:
        if not isinstance(body, dict):
            return ""
        data = str(body.get("data", "")).strip()
        if not data:
            return ""
        padding = "=" * (-len(data) % 4)
        try:
            return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace").strip()
        except Exception:
            return ""
