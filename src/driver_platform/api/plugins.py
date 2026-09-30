"""插件路由：只读的 ``GET /v1/plugins``。

装载/卸载**不**暴露成 HTTP：那等于给调用方一个远程代码执行面。
阶段 4 加了凭证与作用域，但这条边界不变——鉴权只决定「能不能调工具」，
「能不能改运行时」继续只属于进程启动配置（阶段 6 的远程插件再单独设计）。

列表按作用域过滤：工具全不可见的插件不出现在列表里（与 ``GET /v1/tools`` 一致）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from driver_platform.plugins.base import PluginManifest
from driver_platform.plugins.host import PluginHost
from driver_platform.security import PrincipalDep

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])


@router.get("", response_model=list[PluginManifest], summary="列出已装载的插件与其工具")
def list_plugins(request: Request, principal: PrincipalDep) -> list[PluginManifest]:
    host: PluginHost = request.app.state.plugins
    visible: list[PluginManifest] = []
    for manifest in host.manifests():
        tools = [tool for tool in manifest.tools if principal.allows(tool.name)]
        if tools:  # 一个工具都看不见的插件不必出现
            visible.append(manifest.model_copy(update={"tools": tools}))
    return visible
