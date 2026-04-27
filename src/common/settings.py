"""
Configuration management module.

Supports layered configuration loading:
1. config.env (common config)
2. config_{env}.env (optional environment-specific overrides)
3. config_local.env (local startup config, not committed to git)

Process environment variables always have the highest priority.

All Nacos-related configs have been removed for the genworker project.
"""
import os
from pathlib import Path
from typing import Any, Optional
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.runtime.runtime_profile import profile_defaults


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def env_field(*, default: Any, env: str, **kwargs: Any):
    """Pydantic v2-compatible env-bound field helper."""
    return Field(default=default, validation_alias=env, **kwargs)


class Settings(BaseSettings):
    """Application configuration with layered env loading.

    Boolean fields intentionally use Pydantic's standard parsing, so
    `1/true/yes/on` (case-insensitive) resolve to True and all other values,
    including empty strings, resolve to False.
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    # Environment
    environment: str = env_field(default="development", env="ENVIRONMENT")
    debug: bool = env_field(default=False, env="DEBUG")
    runtime_profile: str = env_field(default="local", env="RUNTIME_PROFILE")
    community_smoke_profile: bool = env_field(
        default=False,
        env="COMMUNITY_SMOKE_PROFILE",
    )

    # HA and circuit breaker config
    failover_enabled: bool = env_field(default=True, env="FAILOVER_ENABLED")
    health_check_interval: int = env_field(default=300, env="HEALTH_CHECK_INTERVAL")
    circuit_failure_threshold: int = env_field(
        default=3, env="CIRCUIT_FAILURE_THRESHOLD"
    )
    circuit_recovery_timeout: int = env_field(
        default=60, env="CIRCUIT_RECOVERY_TIMEOUT"
    )
    circuit_success_threshold: int = env_field(
        default=2, env="CIRCUIT_SUCCESS_THRESHOLD"
    )

    # Agent config
    agent_max_iterations: int = env_field(default=10, env="AGENT_MAX_ITERATIONS")
    agent_timeout: int = env_field(default=300, env="AGENT_TIMEOUT")

    # ReAct Agent config
    react_agent_max_tool_rounds: int = env_field(
        default=10, env="REACT_AGENT_MAX_TOOL_ROUNDS"
    )

    # MCP config
    mcp_server_name: str = env_field(default="genworker-mcp", env="MCP_SERVER_NAME")
    mcp_server_version: str = env_field(default="0.1.0", env="MCP_SERVER_VERSION")
    mcp_default_category: str = env_field(default="global", env="MCP_DEFAULT_CATEGORY")
    mcp_auto_load_dependencies: bool = env_field(
        default=True, env="MCP_AUTO_LOAD_DEPENDENCIES"
    )

    # Logging config
    log_dir: str = env_field(default="logs", env="LOG_DIR")
    log_level: str = env_field(default="INFO", env="LOG_LEVEL")
    log_console_output: bool = env_field(default=True, env="LOG_CONSOLE_OUTPUT")
    log_file_output: bool = env_field(default=True, env="LOG_FILE_OUTPUT")
    log_separate_levels: bool = env_field(default=True, env="LOG_SEPARATE_LEVELS")
    log_rotation: str = env_field(default="daily", env="LOG_ROTATION")
    log_backup_count: int = env_field(default=30, env="LOG_BACKUP_COUNT")
    log_buffer_capacity: int = env_field(default=1000, env="LOG_BUFFER_CAPACITY")
    log_flush_interval: float = env_field(default=5.0, env="LOG_FLUSH_INTERVAL")
    log_format: str = env_field(
        default="{yyyy-MM-dd HH:mm:ss.SSS} - [%level] - %relpath - %msg%n",
        env="LOG_FORMAT",
    )
    log_date_format: str = env_field(default="%Y-%m-%d %H:%M:%S", env="LOG_DATE_FORMAT")
    log_console_format: str = env_field(
        default="{yyyy-MM-dd HH:mm:ss} - [%level] - %relpath - %msg%n",
        env="LOG_CONSOLE_FORMAT",
    )

    # HTTP service config
    http_enabled: bool = env_field(default=True, env="HTTP_ENABLED")
    http_host: str = env_field(default="0.0.0.0", env="HTTP_HOST")
    http_port: int = env_field(default=8000, env="HTTP_PORT")
    http_workers: int = env_field(default=1, env="HTTP_WORKERS")

    # Service info
    service_name: str = env_field(default="genworker", env="SERVICE_NAME")
    service_version: str = env_field(default="0.1.0", env="SERVICE_VERSION")
    api_bearer_token: str = env_field(default="", env="API_BEARER_TOKEN")
    api_key: str = env_field(default="", env="API_KEY")
    api_worker_scope: str = env_field(default="*", env="API_WORKER_SCOPE")

    # Redis config
    redis_enabled: bool = env_field(default=False, env="REDIS_ENABLED")
    redis_host: str = env_field(default="localhost", env="REDIS_HOST")
    redis_port: int = env_field(default=6379, env="REDIS_PORT")
    redis_password: Optional[str] = env_field(default=None, env="REDIS_PASSWORD")
    redis_database: int = env_field(default=0, env="REDIS_DATABASE")
    redis_ssl: bool = env_field(default=False, env="REDIS_SSL")
    redis_pool_max_connections: int = env_field(
        default=200, env="REDIS_POOL_MAX_CONNECTIONS"
    )
    redis_timeout: int = env_field(default=10000, env="REDIS_TIMEOUT")
    redis_connect_timeout: int = env_field(default=15000, env="REDIS_CONNECT_TIMEOUT")

    # MySQL config
    mysql_enabled: bool = env_field(default=False, env="MYSQL_ENABLED")
    mysql_host: str = env_field(default="localhost", env="MYSQL_HOST")
    mysql_port: int = env_field(default=3306, env="MYSQL_PORT")
    mysql_database: str = env_field(default="", env="MYSQL_DATABASE")
    mysql_user: str = env_field(default="root", env="MYSQL_USER")
    mysql_password: Optional[str] = env_field(default=None, env="MYSQL_PASSWORD")
    mysql_pool_min_size: int = env_field(default=2, env="MYSQL_POOL_MIN_SIZE")
    mysql_pool_max_size: int = env_field(default=10, env="MYSQL_POOL_MAX_SIZE")
    mysql_connect_timeout: int = env_field(default=10, env="MYSQL_CONNECT_TIMEOUT")
    mysql_command_timeout: int = env_field(default=30, env="MYSQL_COMMAND_TIMEOUT")
    mysql_ssl_enabled: bool = env_field(default=False, env="MYSQL_SSL_ENABLED")
    mysql_ssl_ca: Optional[str] = env_field(default=None, env="MYSQL_SSL_CA")
    mysql_ssl_cert: Optional[str] = env_field(default=None, env="MYSQL_SSL_CERT")
    mysql_ssl_key: Optional[str] = env_field(default=None, env="MYSQL_SSL_KEY")
    mysql_charset: str = env_field(default="utf8mb4", env="MYSQL_CHARSET")
    mysql_autocommit: bool = env_field(default=True, env="MYSQL_AUTOCOMMIT")
    mysql_max_retries: int = env_field(default=3, env="MYSQL_MAX_RETRIES")
    mysql_retry_delay: float = env_field(default=1.0, env="MYSQL_RETRY_DELAY")

    # Embedding config
    embedding_provider: str = env_field(default="openai", env="EMBEDDING_PROVIDER")
    embedding_model: str = env_field(
        default="text-embedding-3-small", env="EMBEDDING_MODEL"
    )

    # Memory system config
    openviking_enabled: bool = env_field(default=False, env="OPENVIKING_ENABLED")
    openviking_endpoint: str = env_field(default="", env="OPENVIKING_ENDPOINT")
    openviking_scope_prefix: str = env_field(
        default="viking://", env="OPENVIKING_SCOPE_PREFIX"
    )
    openviking_timeout_seconds: float = env_field(
        default=5.0, env="OPENVIKING_TIMEOUT_SECONDS"
    )

    # IM channel runtime config
    im_channel_enabled: bool = env_field(default=False, env="IM_CHANNEL_ENABLED")
    im_channel_reconnect_interval: int = env_field(
        default=30, env="IM_CHANNEL_RECONNECT_INTERVAL"
    )
    im_channel_reconnect_max_retries: int = env_field(
        default=10, env="IM_CHANNEL_RECONNECT_MAX_RETRIES"
    )
    im_channel_reconnect_jitter_ratio: float = env_field(
        default=0.2, env="IM_CHANNEL_RECONNECT_JITTER_RATIO"
    )
    im_channel_stream_throttle_ms: int = env_field(
        default=500, env="IM_CHANNEL_STREAM_THROTTLE_MS"
    )
    bash_sandbox_mode: str = env_field(
        default="subprocess", env="BASH_SANDBOX_MODE"
    )
    code_exec_timeout_seconds: int = env_field(
        default=300, env="CODE_EXEC_TIMEOUT_SECONDS"
    )
    code_exec_max_timeout_seconds: int = env_field(
        default=600, env="CODE_EXEC_MAX_TIMEOUT_SECONDS"
    )
    code_exec_max_tool_calls: int = env_field(
        default=50, env="CODE_EXEC_MAX_TOOL_CALLS"
    )
    code_exec_inline_size_limit_bytes: int = env_field(
        default=8192, env="CODE_EXEC_INLINE_SIZE_LIMIT_BYTES"
    )
    script_tool_dir: str = env_field(
        default="~/.genworker/script_tools", env="SCRIPT_TOOL_DIR"
    )
    heartbeat_interval_minutes: int = env_field(
        default=5, env="HEARTBEAT_INTERVAL_MINUTES"
    )
    heartbeat_processing_timeout_minutes: int = env_field(
        default=10, env="HEARTBEAT_PROCESSING_TIMEOUT_MINUTES"
    )
    heartbeat_goal_task_actions: str = env_field(
        default="escalate,recover,investigate",
        env="HEARTBEAT_GOAL_TASK_ACTIONS",
    )
    heartbeat_goal_isolated_actions: str = env_field(
        default="replan,deep_review",
        env="HEARTBEAT_GOAL_ISOLATED_ACTIONS",
    )
    heartbeat_goal_isolated_deviation_threshold: float = env_field(
        default=0.9,
        env="HEARTBEAT_GOAL_ISOLATED_DEVIATION_THRESHOLD",
    )
    persona_auto_reload_enabled: bool = env_field(
        default=False, env="PERSONA_AUTO_RELOAD_ENABLED"
    )
    persona_auto_reload_interval_seconds: float = env_field(
        default=2.0, env="PERSONA_AUTO_RELOAD_INTERVAL_SECONDS"
    )
    persona_auto_reload_debounce_seconds: float = env_field(
        default=1.0, env="PERSONA_AUTO_RELOAD_DEBOUNCE_SECONDS"
    )
    # Validators
    @field_validator('log_dir')
    @classmethod
    def validate_log_dir(cls, v):
        """Validate and expand log directory path."""
        if not v:
            return "logs"
        expanded_path = os.path.expandvars(v)
        expanded_path = os.path.expanduser(expanded_path)
        if not os.path.isabs(expanded_path):
            expanded_path = str(_PROJECT_ROOT / expanded_path)
        return expanded_path

    @field_validator("runtime_profile")
    @classmethod
    def validate_runtime_profile(cls, value: str) -> str:
        """Runtime profile must be a non-empty string."""
        resolved = str(value).strip()
        if not resolved:
            raise ValueError("RUNTIME_PROFILE must not be empty")
        return resolved

    @model_validator(mode="after")
    def apply_runtime_profile_defaults(self) -> "Settings":
        """Apply profile defaults unless a toggle was explicitly configured."""
        defaults = profile_defaults(self.runtime_profile)
        if defaults is None:
            return self

        env_bindings = {
            "redis_enabled": "REDIS_ENABLED",
            "mysql_enabled": "MYSQL_ENABLED",
            "openviking_enabled": "OPENVIKING_ENABLED",
            "im_channel_enabled": "IM_CHANNEL_ENABLED",
        }
        profile_defaults_map = dict(defaults)
        profile_defaults_map.setdefault("im_channel_enabled", False)
        for field_name, env_name in env_bindings.items():
            if os.getenv(env_name) is not None:
                continue
            if field_name in profile_defaults_map:
                setattr(self, field_name, bool(profile_defaults_map[field_name]))
        return self


# Global settings singleton
_settings = None


def resolve_env_file(env_file: Optional[str] = None) -> str:
    """Resolve the effective environment-specific config filename."""
    if env_file is not None:
        return env_file

    env = os.getenv("ENVIRONMENT") or os.getenv("ENV", "local")
    env_file_map = {
        "local": "config_local.env",
        "dev": "config_local.env",
        "development": "config_local.env",
        "test": "config_test.env",
        "testing": "config_test.env",
        "prod": "config_prod.env",
        "production": "config_prod.env",
    }
    return env_file_map.get(env.lower(), "config_local.env")


def load_layered_env(env_file: Optional[str] = None) -> str:
    """
    Load layered dotenv config without overriding existing process env vars.

    File precedence remains:
    config.env < resolved env file < config_local.env

    Process environment variables keep the highest priority.
    """
    resolved_env_file = resolve_env_file(env_file)
    if os.getenv("_CONFIG_LOADED"):
        return resolved_env_file

    from dotenv import dotenv_values

    config_dir = _PROJECT_ROOT / "configs"
    merged: dict[str, str] = {}

    filenames = ["config.env", resolved_env_file]
    if resolved_env_file != "config_local.env":
        filenames.append("config_local.env")

    for filename in filenames:
        path = config_dir / filename
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None:
                merged[key] = value

    for key, value in merged.items():
        os.environ.setdefault(key, value)

    os.environ["_CONFIG_LOADED"] = "1"
    return resolved_env_file


def get_settings(env_file: Optional[str] = None) -> Settings:
    """
    Get settings instance with layered config loading.

    Loading order:
    1. config.env (common config)
    2. resolved env file (optional environment-specific overrides)
    3. config_local.env (local startup config, not committed to git)
    """
    global _settings
    if _settings is None:
        load_layered_env(env_file)
        _settings = Settings()
    return _settings


def reload_settings(env_file: str) -> Settings:
    """Reload settings from a specific config file."""
    global _settings
    _settings = None
    os.environ.pop("_CONFIG_LOADED", None)
    return get_settings(env_file)
