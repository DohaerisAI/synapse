from __future__ import annotations

from pathlib import Path
import json

from .schema import (
    AgentConfig,
    AppConfig,
    ExecutionConfig,
    FilesystemConfig,
    GWSConfig,
    HeartbeatConfig,
    JobsConfig,
    MCPConfig,
    ProviderConfig,
    SlackConfig,
    TelegramConfig,
)

CONFIG_FIELDS = [
    "AGENT_NAME",
    "AGENT_EXTRA_INSTRUCTIONS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_POLLING_ENABLED",
    "TELEGRAM_POLL_INTERVAL",
    "TELEGRAM_REACTIONS_ENABLED",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_SOCKET_MODE",
    "SLACK_BOT_USER_ID",
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
    "EXECUTION_ISOLATED_ENABLED",
    "SKILL_AUTO_INSTALL_DEPS",
    "EXECUTION_ENABLE_LIVE_ANALYZE_NL_ROUTER",
    "EXECUTION_DOCKER_IMAGE",
    "EXECUTION_DOCKER_ALLOW_NETWORK",
    "EXECUTION_DOCKER_MOUNT_WORKSPACE",
    "EXECUTION_TIMEOUT_SECONDS",
    "EXECUTION_MAX_OUTPUT_BYTES",
    "FS_ALLOW_ABSOLUTE",
    "FS_REQUIRE_APPROVAL",
    "JOB_MAX_CONCURRENCY",
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
    "PRICING_JSON",
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


def _load_mcp_config(root: Path) -> MCPConfig:
    """Load MCP configuration from mcp.yaml in the root directory."""
    mcp_path = root / "mcp.yaml"
    if not mcp_path.exists():
        return MCPConfig()
    try:
        import yaml
        data = yaml.safe_load(mcp_path.read_text(encoding="utf-8"))
    except Exception:
        return MCPConfig()
    if not isinstance(data, dict):
        return MCPConfig()
    return MCPConfig.model_validate(data)


def _load_fallback_config(root: Path) -> dict[str, object]:
    config_path = AppConfig.from_root(root).paths.fallback_config_path
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_config(root: Path, env: dict[str, str]) -> AppConfig:
    app = AppConfig.from_root(root)
    fallback = _load_fallback_config(root)
    app.agent = AgentConfig(
        name=env.get("AGENT_NAME", "Agent"),
        extra_instructions=_env_text(env, "AGENT_EXTRA_INSTRUCTIONS", ""),
    )
    app.telegram = TelegramConfig(
        bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        polling_enabled=_env_bool(env, "TELEGRAM_POLLING_ENABLED"),
        poll_interval=_env_float(env, "TELEGRAM_POLL_INTERVAL", 2.0),
        reactions_enabled=_env_bool(env, "TELEGRAM_REACTIONS_ENABLED", True),
    )
    slack_app_token = env.get("SLACK_APP_TOKEN", "")
    slack_socket_mode_raw = env.get("SLACK_SOCKET_MODE", "").strip().lower()
    if slack_socket_mode_raw:
        slack_socket_mode: bool | None = slack_socket_mode_raw in {"1", "true", "yes", "on"}
    else:
        slack_socket_mode = bool(slack_app_token) or None
    app.slack = SlackConfig(
        bot_token=env.get("SLACK_BOT_TOKEN", ""),
        app_token=slack_app_token,
        signing_secret=env.get("SLACK_SIGNING_SECRET", ""),
        socket_mode=slack_socket_mode,
        bot_user_id=env.get("SLACK_BOT_USER_ID", ""),
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
        custom_base_url=env.get("CUSTOM_API_BASE_URL", ""),
        custom_api_key=env.get("CUSTOM_API_KEY", ""),
        custom_model=env.get("CUSTOM_API_MODEL", ""),
    )
    app.heartbeat = HeartbeatConfig(
        enabled=_env_bool(env, "HEARTBEAT_ENABLED"),
        every_minutes=_env_int(env, "HEARTBEAT_EVERY_MINUTES", 10),
        target=env.get("HEARTBEAT_TARGET", "last").strip() or "last",
        ack_mode=env.get("HEARTBEAT_ACK_MODE", "silent_ok").strip() or "silent_ok",
        active_hours=env.get("HEARTBEAT_ACTIVE_HOURS", "").strip(),
        max_chars=_env_int(env, "HEARTBEAT_MAX_CHARS", 400),
    )
    app.execution = ExecutionConfig(
        isolated_execution_enabled=_env_bool(env, "EXECUTION_ISOLATED_ENABLED"),
        skill_auto_install_deps=_env_bool(env, "SKILL_AUTO_INSTALL_DEPS"),
        enable_live_analyze_nl_router=_env_bool(env, "EXECUTION_ENABLE_LIVE_ANALYZE_NL_ROUTER", False),
        docker_image=env.get("EXECUTION_DOCKER_IMAGE", "python:3.11-slim").strip() or "python:3.11-slim",
        docker_allow_network=_env_bool(env, "EXECUTION_DOCKER_ALLOW_NETWORK"),
        docker_mount_workspace=_env_bool(env, "EXECUTION_DOCKER_MOUNT_WORKSPACE", True),
        timeout_seconds=_env_int(env, "EXECUTION_TIMEOUT_SECONDS", 60),
        max_output_bytes=_env_int(env, "EXECUTION_MAX_OUTPUT_BYTES", 64 * 1024),
    )
    app.filesystem = FilesystemConfig(
        allow_absolute=_env_bool(env, "FS_ALLOW_ABSOLUTE"),
        require_approval=_env_bool(env, "FS_REQUIRE_APPROVAL"),
    )
    app.jobs = JobsConfig(
        max_concurrency=max(1, _env_int(env, "JOB_MAX_CONCURRENCY", 1)),
    )
    app.mcp = _load_mcp_config(root)
    raw_pricing = fallback.get("pricing", {})
    if env.get("PRICING_JSON", "").strip():
        from ..usage import parse_pricing_json

        app.pricing = parse_pricing_json(env["PRICING_JSON"])
    else:
        from ..usage import normalize_pricing

        app.pricing = normalize_pricing(raw_pricing)
    return app
