from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .app import create_app
from .runtime import build_runtime
from .setup_flow import configure, doctor_json, doctor_snapshot, onboard, render_doctor
from .tui import run_tui


def main() -> None:
    parser = argparse.ArgumentParser(prog="synapse")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "tui", "onboard", "configure", "doctor", "plugins"])
    parser.add_argument("--root", default=".", help="project root for runtime state")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--refresh", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if args.command == "onboard":
        print(onboard(root, dict(os.environ)))
        return
    if args.command == "configure":
        print(configure(root, dict(os.environ)))
        return
    if args.command == "doctor":
        snapshot = doctor_snapshot(root, dict(os.environ))
        print(doctor_json(snapshot) if args.json else render_doctor(snapshot))
        return
    if args.command == "plugins":
        from .plugins import discover_plugins
        search_paths = [root / "plugins", root / "extensions"]
        manifests = discover_plugins(*search_paths)
        if not manifests:
            print("No plugins discovered.")
        else:
            for manifest in manifests:
                print(f"- {manifest.id} ({manifest.kind.value}): {manifest.description or manifest.name}")
        return
    if args.command == "tui":
        runtime = build_runtime(root)
        run_tui(runtime, refresh_interval=args.refresh, once=args.once)
        return

    uvicorn.run(create_app(root=root), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
