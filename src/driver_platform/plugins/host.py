"""插件宿主：生命周期与原子性，不碰任何具体插件、也不碰 SQL。

装载的原子性：任一工具注册失败（例如工具名撞上内置 ``echo``）就回滚已注册的工具
并调用 ``teardown()``——绝不留下「装载了一半」的插件，否则后面每个请求都在
面对一个不存在完整形态的运行时。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from driver_platform.errors import (
    InvalidPluginError,
    PlatformError,
    PluginLoadError,
    PluginNotFoundError,
)
from driver_platform.plugins.base import (
    PLUGIN_ID_PATTERN,
    PLUGIN_VERSION_PATTERN,
    Plugin,
    PluginManifest,
    PluginToolSummary,
)
from driver_platform.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _LoadedPlugin:
    plugin: Plugin
    tool_names: list[str]


class PluginHost:
    """插件 → 工具注册表之间的唯一通道。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._plugins: dict[str, _LoadedPlugin] = {}

    def load(self, plugin: Plugin) -> None:
        self._validate(plugin)
        if plugin.plugin_id in self._plugins:
            raise PluginLoadError(f"插件已装载：{plugin.plugin_id}")

        try:
            plugin.setup()
            tools = plugin.tools()
        except PlatformError:
            # 初始化失败的收尾同样交给 teardown：插件可能已经分配了一半资源。
            self._rollback(plugin, [])
            raise
        except Exception as exc:  # 插件初始化炸了 → 统一成装载失败，附带原因
            self._rollback(plugin, [])
            raise PluginLoadError(f"插件 {plugin.plugin_id} 初始化失败：{exc}") from exc

        registered: list[str] = []
        try:
            for tool in tools:
                self._registry.register(tool)
                registered.append(tool.name)
        except Exception as exc:
            self._rollback(plugin, registered)
            raise PluginLoadError(f"插件 {plugin.plugin_id} 装载失败：{_reason(exc)}") from exc

        self._plugins[plugin.plugin_id] = _LoadedPlugin(plugin, registered)

    def unload(self, plugin_id: str) -> None:
        loaded = self._plugins.get(plugin_id)
        if loaded is None:
            raise PluginNotFoundError(f"未装载的插件：{plugin_id}")

        # 先摘账再收尾：teardown 抛错时工具也已下线、插件也已除名，
        # 不会留下一个「还在名单上但工具已经没了」的半卸载状态。
        del self._plugins[plugin_id]
        for name in reversed(loaded.tool_names):
            if name in self._registry:  # 被别处摘掉过也不影响卸载
                self._registry.unregister(name)
        loaded.plugin.teardown()

    def get(self, plugin_id: str) -> Plugin:
        """按 id 取已装载的插件实例（管理接口用它判断「这是不是一个远程插件」）。"""
        loaded = self._plugins.get(plugin_id)
        if loaded is None:
            raise PluginNotFoundError(f"未装载的插件：{plugin_id}")
        return loaded.plugin

    def close(self) -> None:
        """逆序卸载全部插件——进程或应用退出时的收尾路径。

        尽力而为：某个插件的 ``teardown()`` 抛错不能拦住其余插件收尾，
        否则第一个坏插件就能让后面所有连接永远不关。
        """
        for plugin_id in reversed(self.plugin_ids()):
            try:
                self.unload(plugin_id)
            except Exception as exc:  # noqa: BLE001 - 退出收尾，逐个留痕后继续
                logger.warning("卸载插件 %s 失败：%s", plugin_id, exc)

    def manifests(self) -> list[PluginManifest]:
        return [
            PluginManifest(
                plugin_id=loaded.plugin.plugin_id,
                version=loaded.plugin.version,
                description=loaded.plugin.description,
                tools=[
                    PluginToolSummary(
                        name=name,
                        contract_version=self._registry.get(name).manifest().contract_version,
                    )
                    for name in loaded.tool_names
                ],
            )
            for _, loaded in sorted(self._plugins.items())
        ]

    def plugin_ids(self) -> list[str]:
        return sorted(self._plugins)

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, plugin_id: object) -> bool:
        return plugin_id in self._plugins

    def _rollback(self, plugin: Plugin, registered: list[str]) -> None:
        for name in reversed(registered):
            if name in self._registry:
                self._registry.unregister(name)
        try:
            plugin.teardown()
        except Exception as exc:  # noqa: BLE001 - 收尾可能抛任意异常，留痕后继续回滚
            logger.warning("插件 %s 回滚时 teardown 失败：%s", plugin.plugin_id, exc)

    @staticmethod
    def _validate(plugin: Plugin) -> None:
        plugin_id = getattr(plugin, "plugin_id", "")
        version = getattr(plugin, "version", "")
        if not PLUGIN_ID_PATTERN.match(plugin_id):
            raise InvalidPluginError(
                f"插件 id 非法：{plugin_id!r}，须匹配 {PLUGIN_ID_PATTERN.pattern}"
            )
        if not PLUGIN_VERSION_PATTERN.match(version):
            raise InvalidPluginError(f"插件版本非法：{version!r}，须形如 MAJOR.MINOR.PATCH")


def _reason(exc: Exception) -> str:
    return exc.message if isinstance(exc, PlatformError) else str(exc)
