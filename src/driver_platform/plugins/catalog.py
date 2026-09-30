"""插件目录：插件 id → 工厂函数。

阶段 6 的「远程插件」也挂到这里，届时只需加一个工厂，宿主与 HTTP 层不用改。
"""

from __future__ import annotations

from collections.abc import Callable

from driver_platform.config import Settings
from driver_platform.errors import InvalidConfigurationError, UnknownPluginError
from driver_platform.plugins.base import Plugin
from driver_platform.plugins.sqlite import SqlitePlugin


def _build_sqlite(settings: Settings) -> Plugin:
    if not settings.sqlite_path:
        raise InvalidConfigurationError(
            "启用 sqlite 插件必须设置 DRIVER_SQLITE_PATH（平台不替调用方猜库在哪里）"
        )
    return SqlitePlugin(settings.sqlite_path)


PLUGIN_FACTORIES: dict[str, Callable[[Settings], Plugin]] = {"sqlite": _build_sqlite}


def build_plugin(plugin_id: str, settings: Settings) -> Plugin:
    try:
        factory = PLUGIN_FACTORIES[plugin_id]
    except KeyError:
        raise UnknownPluginError(
            f"未知插件：{plugin_id}，可用插件：{', '.join(available_plugins()) or '（无）'}"
        ) from None
    return factory(settings)


def available_plugins() -> list[str]:
    return sorted(PLUGIN_FACTORIES)
