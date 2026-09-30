"""工具子系统：契约（base）、注册表（registry）、内置工具。

内置工具是「不依赖任何插件就该在」的最小集合；数据库之类的能力一律走插件，
免得 ``create_app()`` 变成一张还在长的清单。
"""

from __future__ import annotations

from driver_platform.tools.base import Tool, ToolManifest
from driver_platform.tools.echo import EchoTool
from driver_platform.tools.registry import ToolRegistry


def register_builtin_tools(registry: ToolRegistry) -> None:
    """把内置工具注册进给定注册表（echo 是调用链路自检用的）。"""
    registry.register(EchoTool())


__all__ = [
    "EchoTool",
    "Tool",
    "ToolManifest",
    "ToolRegistry",
    "register_builtin_tools",
]
