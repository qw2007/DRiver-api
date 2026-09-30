"""配置外部化测试：默认不装任何插件，配置写错就起不来。"""

from __future__ import annotations

import pytest

from driver_platform.config import ENV_PLUGINS, ENV_PROTOCOLS, ENV_SQLITE_PATH, Settings
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
