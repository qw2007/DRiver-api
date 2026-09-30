"""HTTP 侧的凭证来源：两个请求头 → secret。

这里只做「凭证从哪来」，判定在 ``security/policy.py``——适配器的职责边界止于翻译，
这一条在阶段 3（协议适配层）已经立过。所以 MCP 适配器复用同一段判定，
只是把来源换成 ``DRIVER_API_KEY`` 环境变量。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

from driver_platform.errors import InvalidCredentialsError
from driver_platform.security.keys import Principal
from driver_platform.security.policy import authenticate

if TYPE_CHECKING:
    from driver_platform.config import Settings

BEARER_PREFIX = "bearer "


def extract_secret(request: Request) -> str | None:
    """``Authorization: Bearer <secret>`` 优先，其次 ``X-API-Key``。

    用了别的 Authorization 方案时报明白话，而不是当成「没带凭证」——
    否则用 curl 写成 ``-H "Authorization: s3cret"`` 的人只会反复确认自己没写错密钥。
    """
    authorization = request.headers.get("Authorization", "").strip()
    if authorization:
        if not authorization.lower().startswith(BEARER_PREFIX):
            scheme = authorization.split(" ", 1)[0]
            raise InvalidCredentialsError(
                f"不支持的 Authorization 方案：{scheme}；"
                f"请用 Bearer <api-key>（或改用 X-API-Key 头）"
            )
        return authorization[len(BEARER_PREFIX) :].strip() or None
    return request.headers.get("X-API-Key", "").strip() or None


def principal_of(request: Request) -> Principal:
    """FastAPI 依赖：受保护端点显式声明它，``/health`` 不声明（探针不该带凭证）。"""
    settings: Settings = request.app.state.settings
    return authenticate(settings, extract_secret(request))


PrincipalDep = Annotated[Principal, Depends(principal_of)]
"""端点签名里写它，就等于声明「这个端点要凭证」。

用 ``Annotated`` 而不是参数默认值 ``= Depends(...)``：FastAPI 两种都行，
但默认值形式会被 lint 的 B008（函数调用出现在默认参数里）拦下来，
而这里不想为了一个惯用写法去改全局规则集。
"""
