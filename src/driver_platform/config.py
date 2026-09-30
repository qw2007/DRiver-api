"""配置外部化：可配项只从环境变量读，代码里不留第二份真相。

阶段 2 只有两个开关：装哪些插件（``DRIVER_PLUGINS``）、SQLite 库在哪
（``DRIVER_SQLITE_PATH``）。默认**一个插件都不装**，起服务不会偷偷连库。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

ENV_PLUGINS = "DRIVER_PLUGINS"
ENV_SQLITE_PATH = "DRIVER_SQLITE_PATH"


@dataclass(frozen=True, slots=True)
class Settings:
    plugins: tuple[str, ...] = ()
    sqlite_path: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        environ = os.environ if env is None else env
        raw = environ.get(ENV_PLUGINS, "")
        return cls(
            plugins=tuple(part.strip() for part in raw.split(",") if part.strip()),
            sqlite_path=environ.get(ENV_SQLITE_PATH) or None,
        )
