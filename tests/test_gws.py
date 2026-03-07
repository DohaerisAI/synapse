import json
import subprocess

from synapse.gws import DEFAULT_GWS_ALLOWED_SERVICES, GWSBridge


def fake_runner(command, *, env, cwd, timeout):  # type: ignore[no-untyped-def]
    if command[-2:] == ["auth", "status"]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "auth_method": "oauth2",
                    "storage": "encrypted",
                    "credential_source": "encrypted_credentials",
                    "encrypted_credentials_exists": True,
                    "client_config_exists": True,
                }
            ),
            stderr="",
        )
    if command[1:5] == ["gmail", "users", "messages", "list"]:
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"messages": [{"id": "m1"}]}), stderr="")
    if command[1:5] == ["gmail", "users", "messages", "get"]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "id": "m1",
                    "snippet": "snippet text",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "sender@example.com"},
                            {"name": "Subject", "value": "Test message"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": "Ym9keSB0ZXh0"},
                    },
                }
            ),
            stderr="",
        )
    return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")


def test_gws_status_reads_cli_output(tmp_path) -> None:
    binary = tmp_path / "gws"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge = GWSBridge(
        enabled=True,
        binary=str(binary),
        env={"PATH": "/bin", "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": str(tmp_path / "cfg")},
        workdir=str(tmp_path),
        runner=fake_runner,
    )
    status = bridge.status()
    assert status["auth_available"] is True
    assert status["credential_source"] == "encrypted_credentials"


def test_gws_preview_builds_core5_command(tmp_path) -> None:
    binary = tmp_path / "gws"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge = GWSBridge(enabled=True, binary=str(binary), env={}, workdir=str(tmp_path), runner=fake_runner)
    preview = bridge.preview_action("gws.sheets.read", {"spreadsheet_id": "sheet-1", "range": "A1:B2"})
    assert "sheets spreadsheets values get" in preview


async def test_gws_execute_respects_enabled_flag(tmp_path) -> None:
    binary = tmp_path / "gws"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge = GWSBridge(enabled=False, binary=str(binary), env={}, workdir=str(tmp_path), runner=fake_runner)
    success, detail, _ = await bridge.execute("gws.auth.status", {})
    assert success is False
    assert "disabled" in detail


def test_default_allowed_services_constant_is_core5() -> None:
    assert DEFAULT_GWS_ALLOWED_SERVICES == "gmail,calendar,drive,docs,sheets"


async def test_gws_execute_latest_mail_fetches_full_message(tmp_path) -> None:
    binary = tmp_path / "gws"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge = GWSBridge(enabled=True, binary=str(binary), env={}, workdir=str(tmp_path), runner=fake_runner)

    success, detail, artifacts = await bridge.execute("gws.gmail.latest", {})

    assert success is True
    assert "latest Gmail message" in detail
    output = artifacts["output"]
    assert output["id"] == "m1"
    assert output["subject"] == "Test message"
    assert output["body_preview"] == "body text"


def test_gws_inspect_accepts_help_commands(tmp_path) -> None:
    binary = tmp_path / "gws"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge = GWSBridge(enabled=True, binary=str(binary), env={}, workdir=str(tmp_path), runner=fake_runner)

    preview = bridge.preview_action("gws.inspect", {"argv": ["gmail", "--help"], "service": "gmail"})

    assert "gmail --help" in preview


async def test_gws_calendar_create_rejects_natural_language_times(tmp_path) -> None:
    binary = tmp_path / "gws"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge = GWSBridge(enabled=True, binary=str(binary), env={}, workdir=str(tmp_path), runner=fake_runner)

    success, detail, artifacts = await bridge.execute(
        "gws.calendar.event.create",
        {
            "summary": "chai time",
            "start": "tomorrow 5:00 PM",
            "end": "tomorrow 5:30 PM",
            "attendees": ["partho.pan71@gmail.com"],
        },
    )

    assert success is False
    assert "RFC3339 start datetime" in detail
    assert artifacts == {}
