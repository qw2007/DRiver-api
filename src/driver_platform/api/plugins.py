"""插件路由：只读的 ``GET /v1/plugins`` + 阶段 6 的远程插件管理端点。

**本地**插件的装载/卸载仍然不暴露成 HTTP：那等于给调用方一个远程代码执行面。
阶段 6 放开的是**远程**插件，性质不同——平台只记住一个 URL，不下载也不执行远端的
代码。但也别装得人畜无害：注册一个 URL 等于让平台替调用方发起请求（SSRF），
所以它有三道独立的门：

1. ``DRIVER_REMOTE_PLUGIN_ENABLED`` 默认**关**（关时不注册任何工具，回答 404
   ``remote_plugin_disabled``——与 ``/metrics`` 的「关就不注册路由」刻意不同，
   因为这条路由的程序性调用方会把默认的 ``{"detail": "Not Found"}`` 读成 URL 写错了）；
2. 必须带凭证（``DRIVER_AUTH_MODE=required`` 时天然如此）；
3. 凭证必须带 ``plugin_admin`` 作用域（或本身就是全权钥匙）。

卸载走的是阶段 2 的 ``PluginHost.unload()``（先摘账再收尾），所以「下线即不可用」
不需要任何新机制：``GET /v1/tools`` 里立刻消失，调用返回 404 ``tool_not_found``。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, field_validator

from driver_platform.config import ENV_REMOTE_PLUGIN_ENABLED, Settings
from driver_platform.errors import (
    PermissionDeniedError,
    PluginNotFoundError,
    RemotePluginDisabledError,
)
from driver_platform.plugins.base import PluginManifest
from driver_platform.plugins.host import PluginHost
from driver_platform.plugins.remote import RemotePlugin, normalize_base_url
from driver_platform.security import PrincipalDep
from driver_platform.security.keys import PLUGIN_ADMIN_SCOPE, Principal

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])


class RemotePluginRequest(BaseModel):
    """注册一个远程插件：只给「叫什么」与「在哪」。

    ``extra="forbid"``：字段名拼错（``pluginId``）必须立刻 422，而不是被静默忽略后
    变成「插件名叫空」这种更难查的错。
    """

    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    base_url: str

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        return normalize_base_url(value)


@router.get("", response_model=list[PluginManifest], summary="列出已装载的插件与其工具")
def list_plugins(request: Request, principal: PrincipalDep) -> list[PluginManifest]:
    host: PluginHost = request.app.state.plugins
    visible: list[PluginManifest] = []
    for manifest in host.manifests():
        tools = [tool for tool in manifest.tools if principal.allows(tool.name)]
        if tools:  # 一个工具都看不见的插件不必出现
            visible.append(manifest.model_copy(update={"tools": tools}))
    return visible


@router.post(
    "/remote",
    response_model=PluginManifest,
    status_code=status.HTTP_201_CREATED,
    summary="运行时注册一个远程插件（需要 plugin_admin 作用域）",
)
def register_remote_plugin(
    payload: RemotePluginRequest, request: Request, principal: PrincipalDep
) -> PluginManifest:
    """拉远端清单并注册它的工具。

    这是**同步**端点：``Plugin.setup()`` 要阻塞式地拉一次清单，FastAPI 会把同步端点
    丢进线程池，所以这次网络等待不会卡住事件循环。
    """
    settings: Settings = request.app.state.settings
    _require_remote_api(settings)
    _require_plugin_admin(principal)
    host: PluginHost = request.app.state.plugins
    host.load(RemotePlugin(payload.plugin_id, payload.base_url, timeout=settings.remote_timeout))
    return _manifest_of(host, payload.plugin_id)


@router.delete(
    "/remote/{plugin_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="卸载一个远程插件（其工具立刻不可用）",
)
def unregister_remote_plugin(plugin_id: str, request: Request, principal: PrincipalDep) -> Response:
    """只卸载远程插件。

    本地插件（``sqlite`` 这类）不走这条路：它们是由启动配置装载的，卸载它们不属于
    「远程插件管理」这件事，所以这里按 404 ``plugin_not_found`` 处理——这个 URL 里的
    资源（一个远程插件）确实不存在。
    """
    settings: Settings = request.app.state.settings
    _require_remote_api(settings)
    _require_plugin_admin(principal)
    host: PluginHost = request.app.state.plugins
    if not isinstance(host.get(plugin_id), RemotePlugin):
        raise PluginNotFoundError(f"{plugin_id} 不是远程插件：本接口只管理远程插件")
    host.unload(plugin_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _require_remote_api(settings: Settings) -> None:
    if not settings.remote_plugin_enabled:
        raise RemotePluginDisabledError(
            f"远程插件管理接口未开启：设 {ENV_REMOTE_PLUGIN_ENABLED}=1 后重启"
            "（默认关，理由见 docs/stages/stage-6.md）"
        )


def _require_plugin_admin(principal: Principal) -> None:
    if not principal.can_manage_plugins:
        raise PermissionDeniedError(
            f"该凭证没有插件管理作用域 {PLUGIN_ADMIN_SCOPE}",
            details=[
                {
                    "type": "permission_denied",
                    "loc": ["scope"],
                    "msg": f"需要作用域：{PLUGIN_ADMIN_SCOPE}",
                }
            ],
        )


def _manifest_of(host: PluginHost, plugin_id: str) -> PluginManifest:
    """取刚装载的那个插件的清单（走宿主的唯一真相，不自己拼一遍）。"""
    for manifest in host.manifests():
        if manifest.plugin_id == plugin_id:
            return manifest
    raise PluginNotFoundError(f"未装载的插件：{plugin_id}")
