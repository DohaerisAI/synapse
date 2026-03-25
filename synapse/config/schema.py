from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..usage import PricingEntry


class TelegramConfig(BaseModel):
    bot_token: str = ""
    polling_enabled: bool = False
    poll_interval: float = 2.0
    reactions_enabled: bool = True


class SlackConfig(BaseModel):
    bot_token: str = ""
    app_token: str = ""
    signing_secret: str = ""
    socket_mode: bool | None = None
    bot_user_id: str = ""


class GWSConfig(BaseModel):
    enabled: bool = False
    binary: str = "gws"
    allowed_services: str = "gmail,calendar,drive,docs,sheets"
    planner_extra_instructions: str = ""


class ProviderConfig(BaseModel):
    azure_endpoint: str = ""
    azure_api_key: str = ""
    azure_model: str = "gpt-5.2-chat"
    azure_deployment: str = ""
    azure_api_version: str = "2024-10-21"
    codex_model: str = "gpt-5.4"
    codex_auth_file: str = ""
    codex_transport: str = ""
    custom_base_url: str = ""
    custom_api_key: str = ""
    custom_model: str = ""


class AgentConfig(BaseModel):
    name: str = "Agent"
    extra_instructions: str = ""
    max_agent_loop_turns: int = 4


class HeartbeatConfig(BaseModel):
    enabled: bool = False
    every_minutes: int = 10
    target: str = "last"
    ack_mode: str = "silent_ok"
    active_hours: str = ""
    max_chars: int = 400


class ExecutionConfig(BaseModel):
    isolated_execution_enabled: bool = False
    skill_auto_install_deps: bool = False
    enable_live_analyze_nl_router: bool = False
    docker_image: str = "python:3.11-slim"
    docker_allow_network: bool = False
    docker_mount_workspace: bool = True
    timeout_seconds: int = 60
    max_output_bytes: int = 64 * 1024


class FilesystemConfig(BaseModel):
    allow_absolute: bool = False
    require_approval: bool = False


class JobsConfig(BaseModel):
    max_concurrency: int = 1


class RuntimePaths(BaseModel):
    root: Path
    data_dir: Path
    memory_dir: Path
    skills_dir: Path
    integrations_dir: Path
    sqlite_path: Path
    auth_config_path: Path
    fallback_config_path: Path

    model_config = {"arbitrary_types_allowed": True}


class MCPAuthConfig(BaseModel):
    auth_type: str = "none"
    token: str = ""
    refresh_url: str = ""
    scopes: list[str] = Field(default_factory=list)


class MCPConnectionConfig(BaseModel):
    server_id: str = ""
    url: str = ""
    auth: MCPAuthConfig = Field(default_factory=MCPAuthConfig)
    enabled: bool = True
    rate_limit: int = 60
    transport: str = "http"  # "http" or "stdio"
    command: str = ""  # subprocess command for stdio, e.g. "npx mcp-remote"


class MCPConfig(BaseModel):
    enabled: bool = False
    connections: list[MCPConnectionConfig] = Field(default_factory=list)


class AppConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    paths: RuntimePaths
    agent: AgentConfig = Field(default_factory=AgentConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    gws: GWSConfig = Field(default_factory=GWSConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    filesystem: FilesystemConfig = Field(default_factory=FilesystemConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    pricing: dict[str, PricingEntry] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def from_root(cls, root: Path) -> AppConfig:
        data_dir = root / "var"
        return cls(
            paths=RuntimePaths(
                root=root,
                data_dir=data_dir,
                memory_dir=root / "memory",
                skills_dir=root / "skills",
                integrations_dir=root / "integrations",
                sqlite_path=data_dir / "runtime.sqlite3",
                auth_config_path=data_dir / "auth-profiles.json",
                fallback_config_path=data_dir / "config.json",
            ),
        )
