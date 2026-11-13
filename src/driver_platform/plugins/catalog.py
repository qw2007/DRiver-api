"""插件目录：插件 id → 工厂函数。

阶段 6 的「远程插件」也挂到这里，届时只需加一个工厂，宿主与 HTTP 层不用改。
（实际上远程插件没走这张表：它是运行时可注册的、要带 URL 与超时，工厂签名对不上——
见 ``plugins/remote.py`` 与 ``api/plugins.py``。）

``Settings`` 只在类型检查期 import：``config`` 需要 ``plugins.remote`` 里的
``DEFAULT_TIMEOUT`` 与 ``normalize_base_url``，运行时在这里 import 会绕成环。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from driver_platform.errors import InvalidConfigurationError, UnknownPluginError
from driver_platform.plugins.base import Plugin
from driver_platform.plugins.pho_note import PhoNotePlugin
from driver_platform.plugins.pho_note.ai import AIConfig
from driver_platform.plugins.pho_note.sync import SyncConfig
from driver_platform.plugins.sqlite import SqlitePlugin

if TYPE_CHECKING:  # 只在类型检查期 import：config 会反过来 import 远程插件（见下）
    from driver_platform.config import Settings


def _build_sqlite(settings: Settings) -> Plugin:
    if not settings.sqlite_path:
        raise InvalidConfigurationError(
            "启用 sqlite 插件必须设置 DRIVER_SQLITE_PATH（平台不替调用方猜库在哪里）"
        )
    return SqlitePlugin(settings.sqlite_path)


def _build_pho_note(settings: Settings) -> Plugin:
    # 目录默认落在用户主目录下：跨平台可写，且不把某台机器的盘符写进代码。
    return PhoNotePlugin(
        settings.notes_dir or str(Path.home() / "PhoNote"),
        ai=AIConfig(
            mode=settings.notes_llm,
            base_url=settings.notes_llm_base_url,
            api_key=settings.notes_llm_api_key,
            model=settings.notes_llm_model,
            max_images=settings.notes_llm_max_images,
        ),
        sync=SyncConfig(
            url=settings.notes_webdav_url,
            user=settings.notes_webdav_user,
            password=settings.notes_webdav_password,
            keep=settings.notes_webdav_keep,
        ),
    )


PLUGIN_FACTORIES: dict[str, Callable[[Settings], Plugin]] = {
    "sqlite": _build_sqlite,
    "pho_note": _build_pho_note,
}


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
