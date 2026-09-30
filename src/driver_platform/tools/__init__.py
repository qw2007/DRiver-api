"""工具子系统：契约（base）、注册表（registry）、内置工具。"""

from __future__ import annotations

from driver_platform.tools.base import Tool, ToolManifest
from driver_platform.tools.echo import EchoTool
from driver_platform.tools.registry import ToolRegistry


def build_default_registry() -> ToolRegistry:
    """进程默认注册表。

    阶段 2 的插件加载、阶段 6 的远程注册都在这里挂载，``create_app()`` 不必知道细节。
    """
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


__all__ = [
    "EchoTool",
    "Tool",
    "ToolManifest",
    "ToolRegistry",
    "build_default_registry",
]
