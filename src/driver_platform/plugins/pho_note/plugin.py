"""PhoNote 插件：一个目录 = 一棵笔记树 + 一个图片缓冲区 + 一个 AI 入口。

与 ``sqlite`` 插件一样，插件只负责「把存储接上、把工具挂上去、退出时收干净」；
业务规则全在 ``store.py`` 与 ``ai.py``，这样 P3 把插件拆成独立 app 时，搬走的是
同一份存储层与同一个模型协议。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from driver_platform.errors import InvalidConfigurationError
from driver_platform.plugins.base import Plugin
from driver_platform.plugins.pho_note.ai import AIConfig, NoteAI, build_provider
from driver_platform.plugins.pho_note.store import Store
from driver_platform.plugins.pho_note.tools import build_tools
from driver_platform.tools.base import Tool


class PhoNotePlugin(Plugin):
    plugin_id = "pho_note"
    version = "0.2.0"
    description = "PhoNote 笔记插件：截图缓冲区、自由文件树、Markdown 笔记与 AI 修订"

    def __init__(self, root: str, ai: AIConfig | None = None) -> None:
        self._root = root
        self._config = ai or AIConfig()
        self._store: Store | None = None
        self._ai: NoteAI | None = None
        self._tools: list[Tool[Any, Any]] = []

    def setup(self) -> None:
        try:
            store = Store(Path(self._root))
        except (sqlite3.Error, OSError) as exc:
            raise InvalidConfigurationError(
                f"无法打开 PhoNote 目录 {Path(self._root).resolve()}：{exc}"
            ) from exc
        # 模型只在真要调用时才连：off 模式建 NoteAI 不花任何代价，
        # 配错的 key/base_url 留到第一次请求时报（否则启动就起不来，改配置还得重启）。
        ai = NoteAI(self._config, provider=build_provider(self._config))
        self._store = store
        self._ai = ai
        self._tools = build_tools(store, ai)

    @property
    def store(self) -> Store | None:
        """REST 层用：插件没装载/已收尾时是 ``None``（调用方据此报 404）。"""
        return self._store

    @property
    def ai(self) -> NoteAI | None:
        return self._ai

    def tools(self) -> list[Tool[Any, Any]]:
        return list(self._tools)

    def teardown(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
        self._ai = None
        self._tools = []
