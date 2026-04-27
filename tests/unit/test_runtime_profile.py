# edition: baseline
from types import SimpleNamespace

from src.runtime.runtime_profile import profile_defaults, runtime_profile_warnings


def test_profile_defaults_returns_known_mapping():
    assert profile_defaults("local") == {
        "redis_enabled": False,
        "mysql_enabled": False,
        "openviking_enabled": False,
    }


def test_runtime_profile_warnings_reports_overrides():
    warnings = runtime_profile_warnings(
        SimpleNamespace(
            runtime_profile="local",
            redis_enabled=True,
            mysql_enabled=False,
            openviking_enabled=False,
        )
    )

    assert warnings == (
        "[Runtime] runtime_profile=local override redis=true default=false",
    )


def test_runtime_profile_warnings_reports_unknown_profile():
    warnings = runtime_profile_warnings(
        SimpleNamespace(
            runtime_profile="mystery",
            redis_enabled=False,
            mysql_enabled=False,
            openviking_enabled=False,
        )
    )

    assert warnings == (
        "[Runtime] runtime_profile=mystery has no declared dependency defaults",
    )
