# edition: baseline
from pathlib import Path

import pytest

from src.common import settings as settings_module
from src.common.settings import Settings


def test_runtime_profile_must_not_be_empty(monkeypatch):
    monkeypatch.setenv("RUNTIME_PROFILE", "  ")

    with pytest.raises(Exception):
        Settings()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_bool_env_values_parse_true(monkeypatch, value):
    monkeypatch.setenv("REDIS_ENABLED", value)

    settings = Settings()

    assert settings.redis_enabled is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "off"])
def test_bool_env_values_parse_false(monkeypatch, value):
    monkeypatch.setenv("REDIS_ENABLED", value)

    settings = Settings()

    assert settings.redis_enabled is False


def test_runtime_foundation_defaults():
    settings = Settings()

    assert settings.runtime_profile == "local"
    assert settings.redis_enabled is False
    assert settings.mysql_enabled is False
    assert settings.openviking_enabled is False


def test_relative_log_dir_resolves_from_project_root(monkeypatch):
    monkeypatch.setenv("LOG_DIR", "logs-alt")

    settings = Settings()

    assert settings.log_dir == str(
        Path("/Users/weiyilan/PycharmProjects/genworker") / "logs-alt"
    )


def test_load_layered_env_does_not_depend_on_current_working_directory(monkeypatch):
    monkeypatch.chdir("/Users/weiyilan/PycharmProjects/genworker/src")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("LOG_DIR", raising=False)
    monkeypatch.delenv("_CONFIG_LOADED", raising=False)

    settings = settings_module.reload_settings("config_local.env")

    assert settings.log_dir == "/Users/weiyilan/logs/genworker"

    settings_module._settings = None
    monkeypatch.delenv("_CONFIG_LOADED", raising=False)
