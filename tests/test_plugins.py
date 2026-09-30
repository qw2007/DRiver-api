"""插件宿主的生命周期测试：装载、卸载、以及**失败时不留半成品**。"""

from __future__ import annotations

import pytest

from driver_platform.config import Settings
from driver_platform.errors import (
    InvalidPluginError,
    PluginLoadError,
    PluginNotFoundError,
    ToolNotFoundError,
)
from driver_platform.plugins.base import Plugin
from driver_platform.plugins.host import PluginHost
from driver_platform.tools import ToolRegistry, register_builtin_tools
from driver_platform.tools.base import Tool
from driver_platform.tools.echo import EchoInput, EchoOutput


class AlphaTool(Tool[EchoInput, EchoOutput]):
    name = "alpha"
    contract_version = "1.0"
    description = "测试用：插件自带的工具"
    input_model = EchoInput
    output_model = EchoOutput

    async def execute(self, arguments: EchoInput) -> EchoOutput:
        return EchoOutput(echo=arguments.message, times=arguments.times)


class EchoClashTool(AlphaTool):
    """故意撞内置工具名，用来验证装载回滚。"""

    name = "echo"


class BetaTool(AlphaTool):
    name = "beta"


class StubPlugin(Plugin):
    plugin_id = "stub"
    version = "1.0.0"
    description = "测试用插件"

    def __init__(
        self,
        tools: list[Tool[object, object]] | None = None,
        *,
        setup_error: Exception | None = None,
        teardown_error: Exception | None = None,
        plugin_id: str = "stub",
        version: str = "1.0.0",
    ) -> None:
        self.plugin_id = plugin_id
        self.version = version
        self._tools = list(tools or [AlphaTool()])
        self._setup_error = setup_error
        self._teardown_error = teardown_error
        self.teardown_calls = 0

    def setup(self) -> None:
        if self._setup_error is not None:
            raise self._setup_error

    def tools(self) -> list[Tool[object, object]]:
        return self._tools

    def teardown(self) -> None:
        self.teardown_calls += 1
        if self._teardown_error is not None:
            raise self._teardown_error


@pytest.fixture
def host() -> PluginHost:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    return PluginHost(registry)


def test_load_registers_plugin_tools(host: PluginHost):
    plugin = StubPlugin([AlphaTool(), BetaTool()])

    host.load(plugin)

    assert host.plugin_ids() == ["stub"]
    assert host._registry.names() == ["alpha", "beta", "echo"]
    assert host._registry.get("alpha") is plugin.tools()[0]


def test_manifest_reports_plugin_and_its_tools(host: PluginHost):
    host.load(StubPlugin([AlphaTool()]))

    manifest = host.manifests()[0]

    assert manifest.plugin_id == "stub"
    assert manifest.version == "1.0.0"
    assert manifest.description == "测试用插件"
    assert [(tool.name, tool.contract_version) for tool in manifest.tools] == [("alpha", "1.0")]


def test_unload_removes_tools_and_tears_down(host: PluginHost):
    plugin = StubPlugin([AlphaTool()])
    host.load(plugin)

    host.unload("stub")

    assert host.plugin_ids() == []
    assert len(host) == 0
    assert host._registry.names() == ["echo"]
    assert plugin.teardown_calls == 1
    with pytest.raises(ToolNotFoundError):
        host._registry.get("alpha")


def test_unload_unknown_plugin_raises(host: PluginHost):
    with pytest.raises(PluginNotFoundError):
        host.unload("ghost")


def test_close_unloads_every_plugin_even_if_a_teardown_fails(host: PluginHost):
    """退出收尾尽力而为：坏插件的 teardown 不能让别的插件留在一个半卸载状态里。"""
    healthy = StubPlugin([AlphaTool()], plugin_id="healthy")
    broken = StubPlugin([BetaTool()], plugin_id="broken", teardown_error=RuntimeError("关不掉"))
    host.load(broken)
    host.load(healthy)

    host.close()

    assert host.plugin_ids() == []
    assert host._registry.names() == ["echo"]
    assert (broken.teardown_calls, healthy.teardown_calls) == (1, 1)


def test_tool_conflict_rolls_back_everything(host: PluginHost):
    """第二个工具撞名时，第一个工具也必须被摘掉——不留半装载状态。"""

    plugin = StubPlugin([AlphaTool(), EchoClashTool()])

    with pytest.raises(PluginLoadError) as excinfo:
        host.load(plugin)

    assert "echo" in excinfo.value.message
    assert host.plugin_ids() == []
    assert host._registry.names() == ["echo"]  # 内置 echo 没被换掉，alpha 也没留下
    assert plugin.teardown_calls == 1


def test_setup_failure_keeps_registry_untouched(host: PluginHost):
    plugin = StubPlugin([AlphaTool()], setup_error=RuntimeError("连不上库"))

    with pytest.raises(PluginLoadError) as excinfo:
        host.load(plugin)

    assert "初始化失败" in excinfo.value.message
    assert host._registry.names() == ["echo"]
    assert plugin.teardown_calls == 1  # 初始化失败的收尾一样交给 teardown


def test_configuration_error_from_setup_is_not_rewrapped(host: PluginHost):
    from driver_platform.errors import InvalidConfigurationError

    plugin = StubPlugin([AlphaTool()], setup_error=InvalidConfigurationError("库不存在"))

    with pytest.raises(InvalidConfigurationError):
        host.load(plugin)


def test_loading_same_plugin_twice_is_rejected(host: PluginHost):
    host.load(StubPlugin([AlphaTool()]))

    with pytest.raises(PluginLoadError):
        host.load(StubPlugin([BetaTool()]))


@pytest.mark.parametrize(
    ("plugin_id", "version"),
    [("Bad-Id", "1.0.0"), ("", "1.0.0"), ("stub", "1.0"), ("stub", "v1.0.0")],
)
def test_invalid_plugin_metadata_is_rejected(host: PluginHost, plugin_id: str, version: str):
    plugin = StubPlugin([AlphaTool()], plugin_id=plugin_id, version=version)

    with pytest.raises(InvalidPluginError):
        host.load(plugin)


def test_app_exposes_plugin_host_and_settings(app):
    from driver_platform.main import create_app

    assert isinstance(create_app(Settings()).state.plugins, PluginHost)
    assert app.state.settings == Settings()
    assert app.state.plugins.plugin_ids() == []
