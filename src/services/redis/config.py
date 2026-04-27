"""Redis connection config model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RedisConfig:
    """Redis config loaded from settings or passed explicitly."""

    host: str = "localhost"
    port: int = 6379
    password: Optional[str] = None
    database: int = 0
    ssl: bool = False

    # Pool config
    pool_max_connections: int = 200

    # Timeouts in milliseconds
    timeout: int = 10000
    connect_timeout: int = 15000

    @classmethod
    def from_settings(cls, settings) -> "RedisConfig":
        """Build config from global settings."""
        return cls(
            host=getattr(settings, "redis_host", "localhost"),
            port=getattr(settings, "redis_port", 6379),
            password=getattr(settings, "redis_password", None),
            database=getattr(settings, "redis_database", 0),
            ssl=getattr(settings, "redis_ssl", False),
            pool_max_connections=getattr(
                settings, "redis_pool_max_connections", 200,
            ),
            timeout=getattr(settings, "redis_timeout", 10000),
            connect_timeout=getattr(settings, "redis_connect_timeout", 15000),
        )
