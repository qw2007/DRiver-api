"""插件目录：插件 id → 工厂函数。

阶段 6 的「远程插件」也挂到这里，届时只需加一个工厂，宿主与 HTTP 层不用改。
（实际上远程插件没走这张表：它是运行时可注册的、要带 URL 与超时，工厂签名对不上——
见 ``plugins/remote.py`` 与 ``api/plugins.py``。）

``Settings`` 只在类型检查期 import：``config`` 需要 ``plugins.remote`` 里的
``DEFAULT_TIMEOUT`` 与 ``normalize_base_url``，运行时在这里 import 会绕成环。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from driver_platform.errors import InvalidConfigurationError, UnknownPluginError
from driver_platform.plugins.base import Plugin
from driver_platform.plugins.sqlite import SqlitePlugin

if TYPE_CHECKING:  # 只在类型检查期 import：config 会反过来 import 远程插件（见下）
    from driver_platform.config import Settings


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
