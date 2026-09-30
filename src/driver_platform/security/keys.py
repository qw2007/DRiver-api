"""凭证与主体：API Key → ``Principal``。

作用域是**允许列表**：``None`` 表示全部工具，否则是一组工具名。默认取严——
漏写一个工具只是少个功能（403 立刻可见），多写一个就是静默的权限泄漏。

本模块是纯数据与解析，不 import 配置与协议（阶段 4 的判定逻辑在 ``policy.py``）。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from driver_platform.errors import InvalidConfigurationError

ENV_API_KEYS = "DRIVER_API_KEYS"
"""服务端持有的钥匙清单。"""

ENV_API_KEY = "DRIVER_API_KEY"
"""MCP stdio 客户端**自己那一把**钥匙（单数：不给它别人的）。"""

ALL_TOOLS = "*"
"""作用域里的通配：``secret:*`` 与不带作用域等价。"""

SCOPE_SEPARATOR = ":"
SCOPE_DELIMITER = "|"
_FORBIDDEN_IN_SECRET = (SCOPE_SEPARATOR, SCOPE_DELIMITER, ",")


@dataclass(frozen=True, slots=True)
class ApiKey:
    """一条配置项：密钥本体 + 它能用哪些工具。"""

    secret: str
    scopes: frozenset[str] | None = None

    @property
    def fingerprint(self) -> str:
        """日志里代表这把钥匙的短标识——**绝不打印密钥本体**。"""
        return hashlib.sha256(self.secret.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class Principal:
    """一次调用的身份：指纹 + 它能用的工具集合。"""

    fingerprint: str
    scopes: frozenset[str] | None = None

    def allows(self, tool: str) -> bool:
        """可见性与权限共用这一个判定，杜绝「列表藏起来、调用却放行」。"""
        return self.scopes is None or tool in self.scopes


ANONYMOUS_FINGERPRINT = "anonymous"
"""开放模式下的身份指纹。"""

ANONYMOUS = Principal(fingerprint=ANONYMOUS_FINGERPRINT, scopes=None)
"""开放模式（``DRIVER_AUTH_MODE=off``）下的匿名全权主体。"""


def principal_for(key: ApiKey) -> Principal:
    return Principal(fingerprint=key.fingerprint, scopes=key.scopes)


def match_key(keys: tuple[ApiKey, ...], secret: str) -> ApiKey | None:
    """常数时间比较，不给「猜密钥前缀」留计时侧信道。"""
    for key in keys:
        if hmac.compare_digest(key.secret, secret):
            return key
    return None


def parse_api_keys(raw: str) -> tuple[ApiKey, ...]:
    """解析 ``DRIVER_API_KEYS``：``secret[, secret:tool1|tool2][, secret:*]``。

    写错一律抛 ``invalid_configuration``（启动即失败），不猜用户意图：
    把 ``secret:foo:bar`` 当成「密钥里含冒号」还是「作用域里含冒号」都说得通，
    而猜错的代价是权限开得比预期大。
    """
    keys: list[ApiKey] = []
    seen: set[str] = set()
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        secret, separator, scope_part = entry.partition(SCOPE_SEPARATOR)
        if not secret:
            raise InvalidConfigurationError(f"{ENV_API_KEYS} 里有一项缺少密钥：{entry!r}")
        forbidden = [char for char in _FORBIDDEN_IN_SECRET if char in secret]
        if forbidden:
            raise InvalidConfigurationError(
                f"{ENV_API_KEYS} 的密钥里不允许出现 {'、'.join(forbidden)}"
                f"（它们用于分隔作用域）：{entry!r}"
            )
        if SCOPE_SEPARATOR in scope_part:
            raise InvalidConfigurationError(f"{ENV_API_KEYS} 有一项出现了多个 ':'：{entry!r}")
        if separator and not scope_part.strip():
            raise InvalidConfigurationError(
                f"{ENV_API_KEYS} 有一项的作用域为空：{entry!r}；全部工具写 {ALL_TOOLS}"
            )
        if secret in seen:
            raise InvalidConfigurationError(
                f"{ENV_API_KEYS} 里有重复密钥（指纹 {ApiKey(secret).fingerprint}）"
            )
        seen.add(secret)
        keys.append(ApiKey(secret=secret, scopes=_parse_scopes(scope_part) if separator else None))
    return tuple(keys)


def _parse_scopes(raw: str) -> frozenset[str] | None:
    names = {name.strip() for name in raw.split(SCOPE_DELIMITER) if name.strip()}
    if not names or ALL_TOOLS in names:
        return None
    return frozenset(names)
