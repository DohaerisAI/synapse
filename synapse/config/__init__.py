from .loader import (
    CONFIG_FIELDS,
    load_config,
    load_env_file,
    merged_runtime_env,
    write_env_file,
)
from .schema import (
    AgentConfig,
    AppConfig,
    GWSConfig,
    HeartbeatConfig,
    ProviderConfig,
    RuntimePaths,
    TelegramConfig,
)

# Backward compatibility: old code imports RuntimeConfig from synapse.config
RuntimeConfig = AppConfig

__all__ = [
    "AgentConfig",
    "AppConfig",
    "CONFIG_FIELDS",
    "GWSConfig",
    "HeartbeatConfig",
    "ProviderConfig",
    "RuntimeConfig",
    "RuntimePaths",
    "TelegramConfig",
    "load_config",
    "load_env_file",
    "merged_runtime_env",
    "write_env_file",
]
