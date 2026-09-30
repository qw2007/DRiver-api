"""配置外部化：可配项只从环境变量读，代码里不留第二份真相。

三个开关：装哪些插件（``DRIVER_PLUGINS``）、SQLite 库在哪（``DRIVER_SQLITE_PATH``）、
开哪些协议适配器（``DRIVER_PROTOCOLS``）。默认**一个插件都不装、一个协议都不开**：
起服务不会偷偷连库，也不会默认把工具清单按某个 Agent 协议的形状暴露出去。

配置写错一律启动即失败（阶段 2 立的规矩）：``DRIVER_PROTOCOLS`` 里出现未支持的协议名，
直接 ``unknown_protocol``，而不是静默忽略到某个请求打进来才 404。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from driver_platform.errors import UnknownProtocolError

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
        )


def _split(raw: str) -> tuple[str, ...]:
    """逗号分隔的开关值：去空白、丢空项，保持书写顺序。"""
    return tuple(part.strip() for part in raw.split(",") if part.strip())
