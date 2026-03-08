"""Main onboarding wizard orchestrator."""

from __future__ import annotations

import os
from pathlib import Path

from ..config import load_env_file, merged_runtime_env
from .finalize import finalize
from .prompter import TerminalPrompter, WizardPrompter
from .steps import (
    step_agent,
    step_gws,
    step_heartbeat,
    step_provider,
    step_server,
    step_telegram,
)


def run_onboarding_wizard(
    root: Path,
    *,
    prompter: WizardPrompter | None = None,
    process_env: dict[str, str] | None = None,
    flow: str = "",
    skip_telegram: bool = False,
    skip_gws: bool = False,
    install_daemon: bool = False,
) -> None:
    """Run the interactive onboarding wizard.

    Args:
        root: Project root directory.
        prompter: Prompter to use (defaults to TerminalPrompter).
        process_env: Process environment (defaults to os.environ).
        flow: "quickstart", "advanced", or "" (prompt user).
        skip_telegram: Skip Telegram step.
        skip_gws: Skip GWS step.
        install_daemon: Install systemd service after setup.
    """
    if prompter is None:
        prompter = TerminalPrompter()
    if process_env is None:
        process_env = dict(os.environ)

    prompter.intro("Synapse Setup Wizard")

    # Security note
    prompter.note(
        "Synapse runs with your credentials. Keep .env.local private and never commit it.",
        title="Security",
    )

    # Detect existing config
    env_path = root / ".env.local"
    env = merged_runtime_env(root, process_env)

    if env_path.exists():
        existing = load_env_file(env_path)
        action = prompter.select(
            "Existing .env.local found. What would you like to do?",
            options=[
                ("Update — keep values, change what you want", "update", "Merge existing config with new answers"),
                ("Reset — start fresh with defaults", "reset", "Discard existing config entirely"),
                ("Keep — use as-is, skip wizard", "keep", "No changes, just run doctor"),
            ],
            default="update",
        )
        if action == "keep":
            prompter.outro("Keeping existing configuration. Run `synapse doctor` to verify.")
            return
        if action == "reset":
            env = dict(process_env)
        else:
            env = {**env, **existing}

    # Flow selection (if not specified via flag)
    if not flow:
        flow = prompter.select(
            "Setup mode:",
            options=[
                ("QuickStart", "quickstart", "Name + provider only, sensible defaults for everything else"),
                ("Advanced", "advanced", "Configure every aspect of the runtime"),
            ],
            default="advanced",
        )

    if flow == "quickstart":
        prompter.note("QuickStart mode: minimal prompts, sensible defaults.", title="Flow")
        env = _quickstart_flow(prompter, env, root)
    else:
        env = _advanced_flow(
            prompter,
            env,
            root,
            skip_telegram=skip_telegram,
            skip_gws=skip_gws,
        )

    # Finalize
    finalize(prompter, root, env, process_env)

    # Daemon install
    if install_daemon:
        from .daemon import install_service

        install_service(prompter, root)


def _quickstart_flow(
    prompter: WizardPrompter,
    env: dict[str, str],
    root: Path,
) -> dict[str, str]:
    """Minimal prompts: just agent name and provider."""
    env = step_agent(prompter, env)
    env = step_provider(prompter, env, root)
    return env


def _advanced_flow(
    prompter: WizardPrompter,
    env: dict[str, str],
    root: Path,
    *,
    skip_telegram: bool = False,
    skip_gws: bool = False,
) -> dict[str, str]:
    """Full interactive flow with all steps."""
    env = step_agent(prompter, env)
    env = step_provider(prompter, env, root)

    if not skip_telegram:
        env = step_telegram(prompter, env)

    if not skip_gws:
        env = step_gws(prompter, env)

    env = step_heartbeat(prompter, env)
    env = step_server(prompter, env)
    return env
