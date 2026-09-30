"""配置外部化：可配项只从环境变量读，代码里不留第二份真相。

五个开关：装哪些插件（``DRIVER_PLUGINS``）、SQLite 库在哪（``DRIVER_SQLITE_PATH``）、
开哪些协议适配器（``DRIVER_PROTOCOLS``）、要不要凭证（``DRIVER_AUTH_MODE``）、
钥匙清单与作用域（``DRIVER_API_KEYS``）。默认**一个插件都不装、一个协议都不开、不强制凭证**：
起服务不会偷偷连库，也不会默认把工具清单按某个 Agent 协议的形状暴露出去。

配置写错一律启动即失败（阶段 2 立的规矩）：未知协议名 → ``unknown_protocol``；
未知鉴权模式、``required`` 却没有钥匙清单 → ``invalid_configuration``。
跨字段校验放在 ``__post_init__``，这样直接 ``Settings(...)``（测试与嵌入方的路径）也逃不掉。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from driver_platform.errors import InvalidConfigurationError, UnknownProtocolError
from driver_platform.security.keys import ENV_API_KEYS, ApiKey, parse_api_keys
from driver_platform.security.policy import (
    AUTH_MODE_OFF,
    AUTH_MODE_REQUIRED,
    AUTH_MODES,
    ENV_AUTH_MODE,
)

ENV_PLUGINS = "DRIVER_PLUGINS"
ENV_SQLITE_PATH = "DRIVER_SQLITE_PATH"
ENV_PROTOCOLS = "DRIVER_PROTOCOLS"

#: 允许出现在 ``DRIVER_PROTOCOLS`` 里的协议名，每个名字对应 ``protocols/`` 下的一个适配器。
PROTOCOL_NAMES = frozenset({"mcp", "openai"})


@dataclass(frozen=True, slots=True)
class Settings:
    plugins: tuple[str, ...] = ()
    sqlite_path: str | None = None
    protocols: tuple[str, ...] = ()
    auth_mode: str = AUTH_MODE_OFF
    api_keys: tuple[ApiKey, ...] = ()

    def __post_init__(self) -> None:
        if self.auth_mode not in AUTH_MODES:
            raise InvalidConfigurationError(
                f"未知鉴权模式：{self.auth_mode!r}；可用：{', '.join(AUTH_MODES)}"
            )
        if self.auth_mode == AUTH_MODE_REQUIRED and not self.api_keys:
            raise InvalidConfigurationError(
                f"{ENV_AUTH_MODE}={AUTH_MODE_REQUIRED} 时必须配置 {ENV_API_KEYS}，否则谁都能调"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        environ = os.environ if env is None else env
        protocols = _split(environ.get(ENV_PROTOCOLS, ""))
        unknown = sorted(set(protocols) - PROTOCOL_NAMES)
        if unknown:
            raise UnknownProtocolError(
                f"未知协议：{', '.join(unknown)}；可用：{', '.join(sorted(PROTOCOL_NAMES))}"
            )
        return cls(
            plugins=_split(environ.get(ENV_PLUGINS, "")),
            sqlite_path=environ.get(ENV_SQLITE_PATH) or None,
            protocols=protocols,
            auth_mode=environ.get(ENV_AUTH_MODE, AUTH_MODE_OFF).strip() or AUTH_MODE_OFF,
            api_keys=parse_api_keys(environ.get(ENV_API_KEYS, "")),
        )


def _split(raw: str) -> tuple[str, ...]:
    """逗号分隔的开关值：去空白、丢空项，保持书写顺序。"""
    return tuple(part.strip() for part in raw.split(",") if part.strip())
