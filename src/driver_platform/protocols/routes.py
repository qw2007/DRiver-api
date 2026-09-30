"""协议在 HTTP 侧暴露的端点。

MCP **不走 HTTP**：阶段 3 只实现 stdio 传输（Agent 直接 spawn 进程），
所以这里只有 OpenAI 的只读导出。不为「两端对称好看」造一个半成品的
MCP HTTP 端点——远程传输要处理会话、鉴权与并发，那是阶段 4 与阶段 6 的题目。

这个模块属于**协议层**（不是核心服务）：「加一家协议」只需要在 ``build_router()``
里 include 一次，``main.py`` 不动。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from driver_platform.config import Settings
from driver_platform.protocols import require_protocol
from driver_platform.protocols.openai_schema import to_openai_tools
from driver_platform.tools import ToolRegistry

router = APIRouter(prefix="/v1/protocols", tags=["protocols"])


@router.get("/openai/tools", summary="导出 OpenAI function calling 工具定义")
def openai_tools(request: Request) -> dict[str, Any]:
    """返回可直接塞进 Chat Completions ``tools`` 参数的数组。

    形状：``{"tools": [{"type": "function", "function": {name, description, parameters}}]}``
    """
    settings: Settings = request.app.state.settings
    require_protocol(settings, "openai")
    registry: ToolRegistry = request.app.state.registry
    return {"tools": to_openai_tools(registry.manifests())}
