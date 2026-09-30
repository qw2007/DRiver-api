"""协议适配层：把注册表里的工具翻译成各家 Agent 协议要的形状。

原则「适配器隔离」：MCP 适配器与 OpenAI Schema 适配器**互不 import**，
各自只依赖 ``driver_platform.tools`` 的契约（``ToolManifest`` 就是对外的唯一真相）。
加一家新协议（LangChain、Anthropic…）只是加一个文件，不动核心服务——
``tests/test_protocol_isolation.py`` 把这条原则钉成了测试。

开关来自 ``DRIVER_PROTOCOLS``（默认空 = 一个都不开）：没启用的协议，
HTTP 侧一律 404 ``protocol_not_enabled``，而不是「能用但没人知道」。
"""

from __future__ import annotations

from fastapi import APIRouter

from driver_platform.config import Settings
from driver_platform.errors import ProtocolNotEnabledError

__all__ = ["build_router", "require_protocol"]


def require_protocol(settings: Settings, name: str) -> None:
    """确认某个协议适配器已按配置启用。

    只认 ``Settings.protocols``；名字合法性在 ``Settings.from_env()`` 就校验过了，
    这里不再重复枚举合法值（启动期已经拦掉未知协议）。
    """
    if name not in settings.protocols:
        enabled = ", ".join(settings.protocols) if settings.protocols else "空"
        raise ProtocolNotEnabledError(
            f"协议 {name} 未启用；请在 DRIVER_PROTOCOLS 中显式开启（当前：{enabled}）"
        )


def build_router() -> APIRouter:
    """协议层对 HTTP 暴露的全部端点。

    核心服务只认这一个工厂（``main.py`` 调它）：加一家协议 = 加一个适配器文件
    并在 ``protocols/routes.py`` 里 include 一次，核心代码不动。

    延迟 import 是刻意的：``routes.py`` 反过来要用本模块的 ``require_protocol``，
    模块级 import 会让「包」和「适配器」互相等对方初始化完。
    """
    from driver_platform.protocols.routes import router

    aggregate = APIRouter()
    aggregate.include_router(router)
    return aggregate
