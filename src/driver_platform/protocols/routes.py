"""协议在 HTTP 侧暴露的端点。

MCP **不走 HTTP**：阶段 3 只实现 stdio 传输（Agent 直接 spawn 进程），
所以这里只有 OpenAI 的只读导出。不为「两端对称好看」造一个半成品的
MCP HTTP 端点——远程传输要处理会话、鉴权与并发，那属于阶段 6（远程插件）。

这个模块属于**协议层**（不是核心服务）：「加一家协议」只需要在 ``build_router()``
里 include 一次，``main.py`` 不动。

阶段 4 起这个端点也要凭证，并且和 ``GET /v1/tools`` 用**同一个**过滤函数——
否则作用域形同虚设：Agent 从 OpenAI 导出里就能看到自己调不了的工具。
依赖（鉴权）先于函数体里的协议开关判定，所以没出示凭证的调用方连
「这个协议没开」都不该知道。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from driver_platform.config import Settings
from driver_platform.protocols import require_protocol
from driver_platform.protocols.openai_schema import to_openai_tools
from driver_platform.security import PrincipalDep, visible_manifests
from driver_platform.tools import ToolRegistry

router = APIRouter(prefix="/v1/protocols", tags=["protocols"])


@router.get("/openai/tools", summary="导出 OpenAI function calling 工具定义")
def openai_tools(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """返回可直接塞进 Chat Completions ``tools`` 参数的数组。

    形状：``{"tools": [{"type": "function", "function": {name, description, parameters}}]}``
    """
    settings: Settings = request.app.state.settings
    require_protocol(settings, "openai")
    registry: ToolRegistry = request.app.state.registry
    return {"tools": to_openai_tools(visible_manifests(registry.manifests(), principal))}
