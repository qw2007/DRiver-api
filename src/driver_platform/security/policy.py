"""授权策略：凭证 → 主体，以及「哪些工具对这次调用可见」。

判定只有这一处，HTTP 与 MCP 都调它——两边各写一份，错误码迟早分叉，
Agent 换个协议就换一套行为（阶段 3 在错误映射上踩过同一个坑）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from driver_platform.errors import InvalidCredentialsError, MissingCredentialsError
from driver_platform.security.keys import (
    ANONYMOUS,
    ENV_API_KEYS,
    Principal,
    match_key,
    principal_for,
)

if TYPE_CHECKING:
    from driver_platform.config import Settings
    from driver_platform.tools.base import ToolManifest

AUTH_MODE_OFF = "off"
AUTH_MODE_REQUIRED = "required"
AUTH_MODES = (AUTH_MODE_OFF, AUTH_MODE_REQUIRED)

ENV_AUTH_MODE = "DRIVER_AUTH_MODE"


def authenticate(settings: Settings, secret: str | None) -> Principal:
    """凭证 → 主体。

    开放模式（``off``）下不带凭证就是匿名全权；但**带了凭证就必须有效**——
    静默忽略一个写错的密钥，会让人以为作用域生效了，而实际是全开。
    """
    if not secret:
        if settings.auth_mode == AUTH_MODE_REQUIRED:
            raise MissingCredentialsError(
                f"缺少凭证：在 Authorization: Bearer 或 X-API-Key 里带上 {ENV_API_KEYS} 中的密钥"
            )
        return ANONYMOUS
    key = match_key(settings.api_keys, secret)
    if key is None:
        raise InvalidCredentialsError("凭证无效")
    return principal_for(key)


def visible_manifests(
    manifests: Iterable[ToolManifest], principal: Principal
) -> list[ToolManifest]:
    """按作用域过滤工具清单：**能力不可见即不可用**。

    过滤与放行共用 ``Principal.allows()``，所以不存在「列表里藏着、调用却成功」。
    """
    return [manifest for manifest in manifests if principal.allows(manifest.name)]
