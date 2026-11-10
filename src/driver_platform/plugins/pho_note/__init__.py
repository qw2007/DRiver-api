"""PhoNote 插件包：只导出插件类，工具与存储按需从子模块取。

不在这里做 ``from .tools import *`` 之类的聚合：包导出的东西越多，
``catalog`` 与测试的 import 面就越大，而它们只需要拿到 ``PhoNotePlugin``。
"""

from __future__ import annotations

from driver_platform.plugins.pho_note.plugin import PhoNotePlugin

__all__ = ["PhoNotePlugin"]
