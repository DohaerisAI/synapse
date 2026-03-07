from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class TelegramConfig(BaseModel):
    bot_token: str = ""
    polling_enabled: bool = False
    poll_interval: float = 2.0


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


class AppConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    paths: RuntimePaths
    agent: AgentConfig = Field(default_factory=AgentConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    gws: GWSConfig = Field(default_factory=GWSConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)

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
