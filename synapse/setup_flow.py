from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from .auth import AuthStore
from .config import AppConfig, CONFIG_FIELDS, load_config, merged_runtime_env, write_env_file
from .gws import DEFAULT_GWS_ALLOWED_SERVICES, GWSBridge


def doctor_snapshot(root: Path, process_env: dict[str, str]) -> dict[str, Any]:
    env = merged_runtime_env(root, process_env)
    config = load_config(root, env)
    auth = AuthStore(config.paths.auth_config_path, config.paths.fallback_config_path, env=env)
    gws = GWSBridge(
        enabled=config.gws.enabled,
        binary=config.gws.binary,
        allowed_services=_allowed_services(env),
        env=env,
        workdir=str(root),
    )
    env_file = root / ".env.local"
    return {
        "root": str(root),
        "env_file": str(env_file),
        "env_file_exists": env_file.exists(),
        "telegram": {
            "token_configured": bool(env.get("TELEGRAM_BOT_TOKEN", "").strip()),
            "polling_enabled": _env_bool(env, "TELEGRAM_POLLING_ENABLED", True),
        },
        "codex": auth.health_view(),
        "gws": gws.status(),
        "heartbeat": {
            "enabled": _env_bool(env, "HEARTBEAT_ENABLED", False),
            "every_minutes": _env_int(env, "HEARTBEAT_EVERY_MINUTES", 10),
        },
        "server_host": env.get("SERVER_HOST", "127.0.0.1"),
        "server_port": _env_int(env, "SERVER_PORT", 8000),
        "port_available": _port_available(
            env.get("SERVER_HOST", "127.0.0.1"),
            _env_int(env, "SERVER_PORT", 8000),
        ),
    }


def onboard(root: Path, process_env: dict[str, str]) -> str:
    env = merged_runtime_env(root, process_env)
    env_path = root / ".env.local"
    values = _with_defaults(env)
    write_env_file(env_path, values)
    snapshot = doctor_snapshot(root, process_env)
    next_steps = []
    if not snapshot["telegram"]["token_configured"]:
        next_steps.append("Set TELEGRAM_BOT_TOKEN in .env.local.")
    if not snapshot["gws"]["installed"]:
        next_steps.append("Install gws: npm install -g @googleworkspace/cli")
    elif not snapshot["gws"]["auth_available"]:
        next_steps.append("Authenticate gws: gws auth setup, then gws auth login -s drive,gmail,calendar,docs,sheets")
    next_steps.append("Start the runtime with: agent-runtime serve")
    return "\n".join(
        [
            "Agent Runtime Onboard",
            f"Root: {root}",
            f"Config file: {env_path}",
            "",
            render_doctor(snapshot),
            "",
            "Next steps:",
            *[f"- {step}" for step in next_steps],
        ]
    )


def configure(root: Path, process_env: dict[str, str]) -> str:
    env = merged_runtime_env(root, process_env)
    env_path = root / ".env.local"
    values = _with_defaults(env)
    write_env_file(env_path, values)
    return "\n".join(
        [
            "Agent Runtime Configure",
            f"Updated: {env_path}",
            "",
            "Tracked fields:",
            *[f"- {field}={values.get(field, '')}" for field in CONFIG_FIELDS],
        ]
    )


def render_doctor(snapshot: dict[str, Any]) -> str:
    codex = snapshot["codex"]
    resolved = codex.get("resolved") or {}
    gws = snapshot["gws"]
    return "\n".join(
        [
            "Doctor",
            f"- Env file exists: {'yes' if snapshot['env_file_exists'] else 'no'}",
            f"- Telegram token configured: {'yes' if snapshot['telegram']['token_configured'] else 'no'}",
            f"- Telegram polling: {'on' if snapshot['telegram']['polling_enabled'] else 'off'}",
            f"- Codex resolved: {resolved.get('provider', 'unresolved')} / {resolved.get('model', '-')}",
            f"- Codex source: {resolved.get('source', 'none')}",
            f"- GWS enabled: {'yes' if gws['enabled'] else 'no'}",
            f"- GWS installed: {'yes' if gws['installed'] else 'no'}",
            f"- GWS auth available: {'yes' if gws['auth_available'] else 'no'}",
            f"- GWS credential source: {gws.get('credential_source', 'none')}",
            f"- Server: {snapshot.get('server_host', '127.0.0.1')}:{snapshot.get('server_port', 8000)}",
            f"- Port available: {'yes' if snapshot.get('port_available', snapshot.get('port_8000_available', False)) else 'no'}",
            f"- Heartbeat: {'enabled' if snapshot['heartbeat']['enabled'] else 'disabled'} every {snapshot['heartbeat']['every_minutes']} min",
        ]
    )


def doctor_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, ensure_ascii=True, sort_keys=True)


def _with_defaults(env: dict[str, str]) -> dict[str, str]:
    values = {field: env.get(field, "") for field in CONFIG_FIELDS}
    defaults = {
        "AGENT_NAME": "Agent",
        "AGENT_EXTRA_INSTRUCTIONS": "",
        "TELEGRAM_POLLING_ENABLED": "1",
        "TELEGRAM_POLL_INTERVAL": "2.0",
        "GWS_ENABLED": "0",
        "GWS_BINARY": "gws",
        "GWS_ALLOWED_SERVICES": DEFAULT_GWS_ALLOWED_SERVICES,
        "GWS_PLANNER_EXTRA_INSTRUCTIONS": "",
        "HEARTBEAT_ENABLED": "0",
        "HEARTBEAT_EVERY_MINUTES": "10",
        "HEARTBEAT_TARGET": "last",
        "HEARTBEAT_ACK_MODE": "silent_ok",
        "HEARTBEAT_ACTIVE_HOURS": "",
        "HEARTBEAT_MAX_CHARS": "400",
        "CODEX_MODEL": "gpt-5.4",
        "CODEX_AUTH_FILE": "",
        "CODEX_TRANSPORT": "responses",
        "AZURE_OPENAI_ENDPOINT": "",
        "AZURE_OPENAI_API_KEY": "",
        "AZURE_OPENAI_MODEL": "gpt-5.2-chat",
        "AZURE_OPENAI_DEPLOYMENT": "",
        "AZURE_OPENAI_API_VERSION": "2024-10-21",
        "CUSTOM_API_BASE_URL": "",
        "CUSTOM_API_KEY": "",
        "CUSTOM_API_MODEL": "",
        "SERVER_HOST": "127.0.0.1",
        "SERVER_PORT": "8000",
    }
    for key, default in defaults.items():
        if not values.get(key, "").strip():
            values[key] = default
    return values


def _port_available(host: str, port: int) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return False
    with sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _env_bool(env: dict[str, str], key: str, default: bool = False) -> bool:
    value = env.get(key, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    value = env.get(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _allowed_services(env: dict[str, str]) -> set[str]:
    raw = env.get("GWS_ALLOWED_SERVICES", DEFAULT_GWS_ALLOWED_SERVICES)
    values = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return values or set(DEFAULT_GWS_ALLOWED_SERVICES.split(","))
