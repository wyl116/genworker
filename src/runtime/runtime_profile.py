"""Runtime profile defaults and override diagnostics."""
from __future__ import annotations

from typing import Any

_KNOWN_PROFILE_DEFAULTS: dict[str, dict[str, bool]] = {
    "local": {
        "redis_enabled": False,
        "mysql_enabled": False,
        "openviking_enabled": False,
    },
    "local_memory": {
        "redis_enabled": False,
        "mysql_enabled": False,
        "openviking_enabled": True,
    },
    "advanced": {
        "redis_enabled": True,
        "mysql_enabled": False,
        "openviking_enabled": False,
    },
    "enterprise": {
        "redis_enabled": True,
        "mysql_enabled": True,
        "openviking_enabled": False,
    },
}


def profile_defaults(runtime_profile: str) -> dict[str, bool] | None:
    """Return known dependency defaults for one runtime profile."""
    return _KNOWN_PROFILE_DEFAULTS.get(str(runtime_profile or "").strip())


def runtime_profile_warnings(settings: Any) -> tuple[str, ...]:
    """Build warnings for unknown profiles and profile/default mismatches."""
    runtime_profile = str(getattr(settings, "runtime_profile", "") or "").strip()
    defaults = profile_defaults(runtime_profile)
    if defaults is None:
        return (
            f"[Runtime] runtime_profile={runtime_profile or '<empty>'} has no declared dependency defaults",
        )

    warnings: list[str] = []
    for field_name, expected in defaults.items():
        actual = bool(getattr(settings, field_name, False))
        if actual != expected:
            field_label = field_name.replace("_enabled", "")
            warnings.append(
                f"[Runtime] runtime_profile={runtime_profile} override {field_label}={str(actual).lower()} default={str(expected).lower()}"
            )
    return tuple(warnings)
