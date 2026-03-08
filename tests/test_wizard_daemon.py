"""Tests for systemd unit generation and service management."""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse.wizard.daemon import SERVICE_NAME, generate_unit, service_status


class TestGenerateUnit:
    def test_contains_required_sections(self, tmp_path: Path):
        unit = generate_unit(tmp_path, python_bin="/usr/bin/python3")
        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit

    def test_working_directory(self, tmp_path: Path):
        unit = generate_unit(tmp_path, python_bin="/usr/bin/python3")
        assert f"WorkingDirectory={tmp_path}" in unit

    def test_exec_start(self, tmp_path: Path):
        unit = generate_unit(tmp_path, python_bin="/usr/bin/python3")
        assert f"ExecStart=/usr/bin/python3 -m synapse serve --root {tmp_path}" in unit

    def test_restart_on_failure(self, tmp_path: Path):
        unit = generate_unit(tmp_path, python_bin="/usr/bin/python3")
        assert "Restart=on-failure" in unit

    def test_default_target(self, tmp_path: Path):
        unit = generate_unit(tmp_path, python_bin="/usr/bin/python3")
        assert "WantedBy=default.target" in unit

    def test_uses_venv_python_if_exists(self, tmp_path: Path):
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("#!/bin/sh\n")

        unit = generate_unit(tmp_path)
        assert str(venv_bin / "python") in unit

    def test_custom_python_bin(self, tmp_path: Path):
        unit = generate_unit(tmp_path, python_bin="/opt/python/bin/python3.12")
        assert "/opt/python/bin/python3.12" in unit

    def test_pythonunbuffered(self, tmp_path: Path):
        unit = generate_unit(tmp_path, python_bin="/usr/bin/python3")
        assert "PYTHONUNBUFFERED=1" in unit


class TestServiceStatus:
    def test_returns_dict(self):
        status = service_status()
        assert isinstance(status, dict)
        assert status["service"] == SERVICE_NAME
        assert "active" in status
        assert "unit_file" in status
        assert "unit_exists" in status

    def test_service_name(self):
        assert SERVICE_NAME == "synapse"
