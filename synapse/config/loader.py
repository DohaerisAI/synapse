from __future__ import annotations

from pathlib import Path

from .schema import (
    AgentConfig,
    AppConfig,
    GWSConfig,
    HeartbeatConfig,
    ProviderConfig,
    TelegramConfig,
)

CONFIG_FIELDS = [
    "AGENT_NAME",
    "AGENT_EXTRA_INSTRUCTIONS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_POLLING_ENABLED",
    "TELEGRAM_POLL_INTERVAL",
    "GWS_ENABLED",
    "GWS_BINARY",
    "GWS_ALLOWED_SERVICES",
    "GWS_PLANNER_EXTRA_INSTRUCTIONS",
    "HEARTBEAT_ENABLED",
    "HEARTBEAT_EVERY_MINUTES",
    "HEARTBEAT_TARGET",
    "HEARTBEAT_ACK_MODE",
    "HEARTBEAT_ACTIVE_HOURS",
    "HEARTBEAT_MAX_CHARS",
    "CODEX_MODEL",
    "CODEX_AUTH_FILE",
    "CODEX_TRANSPORT",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_MODEL",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION",
    "CUSTOM_API_BASE_URL",
    "CUSTOM_API_KEY",
    "CUSTOM_API_MODEL",
    "SERVER_HOST",
    "SERVER_PORT",
]


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{field}={values.get(field, '')}" for field in CONFIG_FIELDS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def merged_runtime_env(root: Path, process_env: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    merged.update(load_env_file(root / ".env"))
    merged.update(load_env_file(root / ".env.local"))
    merged.update(process_env)
    return merged


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


def _env_float(env: dict[str, str], key: str, default: float) -> float:
    value = env.get(key, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_text(env: dict[str, str], key: str, default: str = "") -> str:
    value = env.get(key, default)
    return value.replace("\\n", "\n").strip()


def load_config(root: Path, env: dict[str, str]) -> AppConfig:
    app = AppConfig.from_root(root)
    app.agent = AgentConfig(
        name=env.get("AGENT_NAME", "Agent"),
        extra_instructions=_env_text(env, "AGENT_EXTRA_INSTRUCTIONS", ""),
    )
    app.telegram = TelegramConfig(
        bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        polling_enabled=_env_bool(env, "TELEGRAM_POLLING_ENABLED"),
        poll_interval=_env_float(env, "TELEGRAM_POLL_INTERVAL", 2.0),
    )
    app.gws = GWSConfig(
        enabled=_env_bool(env, "GWS_ENABLED"),
        binary=env.get("GWS_BINARY", "gws"),
        allowed_services=env.get("GWS_ALLOWED_SERVICES", "gmail,calendar,drive,docs,sheets"),
        planner_extra_instructions=_env_text(env, "GWS_PLANNER_EXTRA_INSTRUCTIONS", ""),
    )
    app.provider = ProviderConfig(
        codex_model=env.get("CODEX_MODEL", "gpt-5.4") or "gpt-5.4",
        codex_auth_file=env.get("CODEX_AUTH_FILE", ""),
        codex_transport=env.get("CODEX_TRANSPORT", ""),
        azure_endpoint=env.get("AZURE_OPENAI_ENDPOINT", ""),
        azure_api_key=env.get("AZURE_OPENAI_API_KEY", ""),
        azure_model=env.get("AZURE_OPENAI_MODEL", "gpt-5.2-chat"),
        azure_deployment=env.get("AZURE_OPENAI_DEPLOYMENT", ""),
        azure_api_version=env.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )
    app.heartbeat = HeartbeatConfig(
        enabled=_env_bool(env, "HEARTBEAT_ENABLED"),
        every_minutes=_env_int(env, "HEARTBEAT_EVERY_MINUTES", 10),
        target=env.get("HEARTBEAT_TARGET", "last").strip() or "last",
        ack_mode=env.get("HEARTBEAT_ACK_MODE", "silent_ok").strip() or "silent_ok",
        active_hours=env.get("HEARTBEAT_ACTIVE_HOURS", "").strip(),
        max_chars=_env_int(env, "HEARTBEAT_MAX_CHARS", 400),
    )
    return app
