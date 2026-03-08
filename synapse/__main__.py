from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .runtime import build_runtime
from .setup_flow import doctor_json, doctor_snapshot, render_doctor
from .tui import run_tui


def main() -> None:
    parser = argparse.ArgumentParser(prog="synapse")
    sub = parser.add_subparsers(dest="command")

    # serve
    serve_p = sub.add_parser("serve", help="Start the runtime server")
    serve_p.add_argument("--root", default=".", help="project root")
    serve_p.add_argument("--host", default=None, help="bind host (default: from config or 127.0.0.1)")
    serve_p.add_argument("--port", type=int, default=None, help="bind port (default: from config or 8000)")

    # tui
    tui_p = sub.add_parser("tui", help="Launch the TUI dashboard")
    tui_p.add_argument("--root", default=".", help="project root")
    tui_p.add_argument("--refresh", type=float, default=2.0)
    tui_p.add_argument("--once", action="store_true")

    # onboard
    onboard_p = sub.add_parser("onboard", help="Interactive setup wizard")
    onboard_p.add_argument("--root", default=".", help="project root")
    onboard_p.add_argument(
        "--flow",
        choices=["quickstart", "advanced"],
        default="advanced",
        help="wizard flow mode",
    )
    onboard_p.add_argument("--install-daemon", action="store_true", help="install systemd service")
    onboard_p.add_argument("--skip-telegram", action="store_true", help="skip Telegram step")
    onboard_p.add_argument("--skip-gws", action="store_true", help="skip GWS step")

    # configure
    configure_p = sub.add_parser("configure", help="Re-run wizard for specific sections")
    configure_p.add_argument("--root", default=".", help="project root")

    # doctor
    doctor_p = sub.add_parser("doctor", help="Health check")
    doctor_p.add_argument("--root", default=".", help="project root")
    doctor_p.add_argument("--json", action="store_true", dest="json_output")

    # plugins
    plugins_p = sub.add_parser("plugins", help="List discovered plugins")
    plugins_p.add_argument("--root", default=".", help="project root")

    # service
    service_p = sub.add_parser("service", help="Manage systemd service")
    service_sub = service_p.add_subparsers(dest="service_action")
    service_install = service_sub.add_parser("install", help="Install systemd user service")
    service_install.add_argument("--root", default=".", help="project root")
    service_sub.add_parser("uninstall", help="Remove systemd user service")
    service_sub.add_parser("status", help="Check service status")

    args = parser.parse_args()
    command = args.command or "serve"

    root = Path(getattr(args, "root", ".")).resolve()

    if command == "onboard":
        from .wizard import run_onboarding_wizard

        try:
            run_onboarding_wizard(
                root,
                flow=args.flow,
                skip_telegram=args.skip_telegram,
                skip_gws=args.skip_gws,
                install_daemon=args.install_daemon,
            )
        except KeyboardInterrupt:
            print("\nWizard cancelled. No changes were made.")
            sys.exit(1)
        return

    if command == "configure":
        from .wizard import run_onboarding_wizard

        try:
            run_onboarding_wizard(root)
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(1)
        return

    if command == "doctor":
        snapshot = doctor_snapshot(root, dict(os.environ))
        print(doctor_json(snapshot) if args.json_output else render_doctor(snapshot))
        return

    if command == "plugins":
        from .plugins import discover_plugins

        search_paths = [root / "plugins", root / "extensions"]
        manifests = discover_plugins(*search_paths)
        if not manifests:
            print("No plugins discovered.")
        else:
            for manifest in manifests:
                print(f"- {manifest.id} ({manifest.kind.value}): {manifest.description or manifest.name}")
        return

    if command == "service":
        _handle_service(args, root)
        return

    if command == "tui":
        runtime = build_runtime(root)
        run_tui(runtime, refresh_interval=args.refresh, once=args.once)
        return

    # Default: serve — resolve host/port from config if not explicitly passed
    from .config import merged_runtime_env

    env = merged_runtime_env(root, dict(os.environ))
    host = args.host if args.host is not None else env.get("SERVER_HOST", "127.0.0.1")
    port = args.port if args.port is not None else int(env.get("SERVER_PORT", "8000") or "8000")
    uvicorn.run(create_app(root=root), host=host, port=port)


def _handle_service(args: argparse.Namespace, root: Path) -> None:
    """Handle service subcommands."""
    from .wizard.daemon import install_service, service_status, uninstall_service
    from .wizard.prompter import TerminalPrompter

    action = getattr(args, "service_action", None)
    if action == "install":
        prompter = TerminalPrompter()
        install_service(prompter, root)
    elif action == "uninstall":
        prompter = TerminalPrompter()
        uninstall_service(prompter)
    elif action == "status":
        status = service_status()
        for key, val in status.items():
            print(f"  {key}: {val}")
    else:
        print("Usage: synapse service {install|uninstall|status}")
        sys.exit(1)


if __name__ == "__main__":
    main()
