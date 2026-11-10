"""PhoNote 插件：一个目录 = 一棵笔记树 + 一个图片缓冲区。

与 ``sqlite`` 插件一样，插件只负责「把存储接上、把工具挂上去、退出时收干净」；
业务规则全在 ``store.py``，这样 P3 把插件拆成独立 app 时，搬走的是同一份存储层。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from driver_platform.errors import InvalidConfigurationError
from driver_platform.plugins.base import Plugin
from driver_platform.plugins.pho_note.store import Store
from driver_platform.plugins.pho_note.tools import build_tools
from driver_platform.tools.base import Tool


class PhoNotePlugin(Plugin):
    plugin_id = "pho_note"
    version = "0.1.0"
    description = "PhoNote 笔记插件：截图缓冲区、自由文件树与 Markdown 笔记（P0）"

    def __init__(self, root: str) -> None:
        self._root = root
        self._store: Store | None = None
        self._tools: list[Tool[Any, Any]] = []

    def setup(self) -> None:
        try:
            store = Store(Path(self._root))
        except (sqlite3.Error, OSError) as exc:
            raise InvalidConfigurationError(
                f"无法打开 PhoNote 目录 {Path(self._root).resolve()}：{exc}"
            ) from exc
        self._store = store
        self._tools = build_tools(store)

    def tools(self) -> list[Tool[Any, Any]]:
        return list(self._tools)

    def teardown(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
        self._tools = []
