"""插件路由：只读的 ``GET /v1/plugins``。

装载/卸载**不**暴露成 HTTP：那等于给调用方一个远程代码执行面，
留到阶段 4（安全权限）与阶段 6（远程插件）再谈鉴权与隔离。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from driver_platform.plugins.base import PluginManifest
from driver_platform.plugins.host import PluginHost

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])


@router.get("", response_model=list[PluginManifest], summary="列出已装载的插件与其工具")
def list_plugins(request: Request) -> list[PluginManifest]:
    host: PluginHost = request.app.state.plugins
    return host.manifests()
