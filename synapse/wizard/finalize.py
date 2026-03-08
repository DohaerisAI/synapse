"""Finalization: write config, ensure directories, run doctor, show summary."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..config import CONFIG_FIELDS, write_env_file
from ..setup_flow import doctor_snapshot, render_doctor

if TYPE_CHECKING:
    from .prompter import WizardPrompter


def finalize(
    prompter: WizardPrompter,
    root: Path,
    env: dict[str, str],
    process_env: dict[str, str],
) -> None:
    """Write .env.local, ensure dirs, run doctor, show summary."""
    # Filter to only CONFIG_FIELDS for the env file
    config_fields_set = set(CONFIG_FIELDS)
    values = {k: v for k, v in env.items() if k in config_fields_set}

    # Write config
    env_path = root / ".env.local"
    write_env_file(env_path, values)
    prompter.note(f"Configuration written to {env_path}", title="Config")

    # Ensure directories
    dirs = ["var", "memory", "skills", "integrations"]
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    prompter.note(f"Ensured directories: {', '.join(dirs)}", title="Dirs")

    # Run doctor
    merged = {**process_env, **env}
    snapshot = doctor_snapshot(root, merged)
    prompter.note(render_doctor(snapshot), title="Health Check")

    # Console URL
    host = env.get("SERVER_HOST", "127.0.0.1")
    port = env.get("SERVER_PORT", "8000")
    console_url = f"http://{host}:{port}/console"

    prompter.outro(
        "\n".join([
            "Setup complete!",
            "",
            "Next steps:",
            f"  synapse serve          Start the runtime",
            f"  synapse doctor         Check health",
            f"  {console_url}  Open console",
        ])
    )
