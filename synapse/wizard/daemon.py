"""Systemd user service generation and management."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .prompter import WizardPrompter

SERVICE_NAME = "synapse"


def generate_unit(root: Path, python_bin: str | None = None) -> str:
    """Generate a systemd user service unit file."""
    if python_bin is None:
        python_bin = _find_python(root)

    return "\n".join([
        "[Unit]",
        "Description=Synapse Agent Runtime",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={root}",
        f"ExecStart={python_bin} -m synapse serve --root {root}",
        "Restart=on-failure",
        "RestartSec=5",
        "Environment=PYTHONUNBUFFERED=1",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def install_service(prompter: WizardPrompter, root: Path) -> bool:
    """Install the systemd user service. Returns True on success."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / f"{SERVICE_NAME}.service"

    unit_content = generate_unit(root)
    unit_path.write_text(unit_content, encoding="utf-8")
    prompter.note(f"Unit file written to {unit_path}", title="Daemon")

    # Reload and enable
    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", SERVICE_NAME],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "start", SERVICE_NAME],
            check=True,
            capture_output=True,
        )
        prompter.note("Service installed, enabled, and started.", title="Daemon")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        prompter.note(f"Could not manage service: {exc}", title="Daemon")
        return False


def uninstall_service(prompter: WizardPrompter) -> bool:
    """Stop, disable, and remove the systemd user service."""
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", SERVICE_NAME],
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "disable", SERVICE_NAME],
            capture_output=True,
        )
    except FileNotFoundError:
        pass

    unit_path = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
    if unit_path.exists():
        unit_path.unlink()
        prompter.note("Service unit removed.", title="Daemon")
    else:
        prompter.note("No service unit found.", title="Daemon")

    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
        )
    except FileNotFoundError:
        pass

    return True


def service_status() -> dict[str, str]:
    """Get current service status as a dict."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
        )
        active = result.stdout.strip()
    except FileNotFoundError:
        active = "systemctl-not-found"

    unit_path = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
    return {
        "service": SERVICE_NAME,
        "active": active,
        "unit_file": str(unit_path),
        "unit_exists": str(unit_path.exists()),
    }


def _find_python(root: Path) -> str:
    """Find the best Python binary (prefer venv)."""
    venv_python = root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    system_python = shutil.which("python3") or shutil.which("python")
    return system_python or "python3"
