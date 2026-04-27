# edition: baseline
from src.common import settings as settings_module
from src.common.settings import Settings


def test_settings_reads_environment_variables(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OPENVIKING_ENDPOINT", "http://openviking.internal")
    monkeypatch.setenv("IM_CHANNEL_ENABLED", "true")

    settings = Settings()

    assert settings.environment == "production"
    assert settings.openviking_endpoint == "http://openviking.internal"
    assert settings.im_channel_enabled is True


def test_get_settings_env_vars_override_env_files(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("LOG_DIR", "/tmp/genworker-test-logs")
    monkeypatch.delenv("_CONFIG_LOADED", raising=False)

    settings = settings_module.reload_settings("config_local.env")

    assert settings.log_dir == "/tmp/genworker-test-logs"

    settings_module._settings = None
    monkeypatch.delenv("_CONFIG_LOADED", raising=False)


def test_get_settings_keeps_env_file_precedence_without_process_override(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("_CONFIG_LOADED", raising=False)

    settings = settings_module.reload_settings("config_local.env")

    assert settings.log_level == "DEBUG"

    settings_module._settings = None
    monkeypatch.delenv("_CONFIG_LOADED", raising=False)
