"""安全权限（阶段 4）：凭证、作用域、可见性。

对外只暴露这一层，协议适配器与业务代码都从这里取：
``Principal``（身份）、``authenticate()``（凭证 → 身份）、
``PrincipalDep``（受保护端点的依赖标注）、``visible_manifests()``（按作用域过滤清单）。
"""

from __future__ import annotations

from driver_platform.security.http import PrincipalDep, extract_secret, principal_of
from driver_platform.security.keys import (
    ALL_TOOLS,
    ANONYMOUS,
    ENV_API_KEY,
    ENV_API_KEYS,
    ApiKey,
    Principal,
    parse_api_keys,
)
from driver_platform.security.policy import (
    AUTH_MODE_OFF,
    AUTH_MODE_REQUIRED,
    AUTH_MODES,
    ENV_AUTH_MODE,
    authenticate,
    visible_manifests,
)

__all__ = [
    "ALL_TOOLS",
    "ANONYMOUS",
    "AUTH_MODES",
    "AUTH_MODE_OFF",
    "AUTH_MODE_REQUIRED",
    "ENV_API_KEY",
    "ENV_API_KEYS",
    "ENV_AUTH_MODE",
    "ApiKey",
    "Principal",
    "PrincipalDep",
    "authenticate",
    "extract_secret",
    "parse_api_keys",
    "principal_of",
    "visible_manifests",
]
