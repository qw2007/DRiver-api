"""配置外部化测试：默认不装任何插件，配置写错就起不来。"""

from __future__ import annotations

import pytest

from driver_platform.config import (
    ENV_LOG_FORMAT,
    ENV_LOG_LEVEL,
    ENV_METRICS_ENABLED,
    ENV_PLUGINS,
    ENV_PROTOCOLS,
    ENV_SQLITE_PATH,
    Settings,
)
from driver_platform.errors import (
    InvalidConfigurationError,
    UnknownPluginError,
    UnknownProtocolError,
)
from driver_platform.main import create_app


def test_defaults_are_empty():
    settings = Settings.from_env({})

    assert settings.plugins == ()
    assert settings.sqlite_path is None


def test_env_parsing_trims_blanks_and_splits_commas():
    settings = Settings.from_env(
        {ENV_PLUGINS: " sqlite , echo ,, ", ENV_SQLITE_PATH: "/tmp/demo.db"}
    )

    assert settings.plugins == ("sqlite", "echo")
    assert settings.sqlite_path == "/tmp/demo.db"


def test_blank_sqlite_path_is_none():
    assert Settings.from_env({ENV_SQLITE_PATH: ""}).sqlite_path is None


def test_default_app_has_no_plugins(client):
    assert client.get("/v1/plugins").json() == []
    assert [item["name"] for item in client.get("/v1/tools").json()] == ["echo"]


def test_unknown_plugin_fails_at_startup():
    with pytest.raises(UnknownPluginError) as excinfo:
        create_app(Settings(plugins=("ghost",)))

    assert "sqlite" in str(excinfo.value)  # 错误信息里给出可用插件


def test_sqlite_plugin_requires_path():
    with pytest.raises(InvalidConfigurationError):
        create_app(Settings(plugins=("sqlite",)))


def test_sqlite_plugin_requires_existing_file(tmp_path):
    missing = tmp_path / "nope.db"

    with pytest.raises(InvalidConfigurationError) as excinfo:
        create_app(Settings(plugins=("sqlite",), sqlite_path=str(missing)))

    assert "不存在" in str(excinfo.value)


def test_protocols_default_to_empty():
    """特性开关默认关：默认实例一个协议适配器都不开。"""
    assert Settings.from_env({}).protocols == ()


def test_protocols_env_parsing():
    settings = Settings.from_env({ENV_PROTOCOLS: " openai , mcp ,, "})

    assert settings.protocols == ("openai", "mcp")


def test_unknown_protocol_fails_at_startup():
    with pytest.raises(UnknownProtocolError) as excinfo:
        Settings.from_env({ENV_PROTOCOLS: "langchain"})

    assert "langchain" in str(excinfo.value)
    assert "mcp" in str(excinfo.value)  # 错误信息里给出可用协议


# --- 阶段 5：可观测性开关 ------------------------------------------------------


def test_metrics_default_to_disabled():
    """特性开关默认关：默认实例连 /metrics 这个端点都不存在（它是无鉴权的）。"""
    assert Settings.from_env({}).metrics_enabled is False


@pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
def test_metrics_truthy_spellings(raw):
    assert Settings.from_env({ENV_METRICS_ENABLED: raw}).metrics_enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "No", "off", ""])
def test_metrics_falsy_spellings(raw):
    assert Settings.from_env({ENV_METRICS_ENABLED: raw}).metrics_enabled is False


def test_typo_in_a_boolean_switch_fails_at_startup():
    """拼错既不当真也不当假：否则人以为自己开了指标，实际上一片空白。"""
    with pytest.raises(InvalidConfigurationError) as excinfo:
        Settings.from_env({ENV_METRICS_ENABLED: "flase"})

    assert ENV_METRICS_ENABLED in str(excinfo.value)


def test_logging_defaults_to_readable_text_at_info():
    settings = Settings.from_env({})

    assert settings.log_level == "INFO"
    assert settings.log_format == "text"


def test_log_level_is_case_insensitive():
    assert Settings.from_env({ENV_LOG_LEVEL: "debug"}).log_level == "debug"


def test_unknown_log_level_fails_at_startup():
    with pytest.raises(InvalidConfigurationError) as excinfo:
        Settings.from_env({ENV_LOG_LEVEL: "SHOUT"})

    assert "SHOUT" in str(excinfo.value)


def test_unknown_log_format_fails_at_startup():
    with pytest.raises(InvalidConfigurationError) as excinfo:
        Settings.from_env({ENV_LOG_FORMAT: "logfmt"})

    assert "logfmt" in str(excinfo.value)


def test_direct_settings_construction_is_validated_too():
    """校验放在 ``__post_init__``：绕过环境变量的嵌入方也逃不掉。"""
    with pytest.raises(InvalidConfigurationError):
        Settings(log_format="yaml")

    with pytest.raises(InvalidConfigurationError):
        Settings(log_level="LOUD")
