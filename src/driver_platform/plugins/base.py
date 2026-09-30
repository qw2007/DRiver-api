"""插件契约。

插件是一个**有身份、有版本、能装载/卸载**的单元，工具只是它对外暴露的产物。
身份与版本用两套编号：插件实现版本 ``MAJOR.MINOR.PATCH``（随实现走），
单个工具的契约版本 ``MAJOR.MINOR``（随对外接口走），两者互不牵连。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from driver_platform.tools.base import Tool

PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PLUGIN_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


class PluginToolSummary(BaseModel):
    """插件拥有哪些工具——只给身份，细节去 ``GET /v1/tools`` 取。"""

    name: str
    contract_version: str


class PluginManifest(BaseModel):
    plugin_id: str
    version: str
    description: str
    tools: list[PluginToolSummary]


class Plugin(ABC):
    plugin_id: str
    version: str
    description: str

    @abstractmethod
    def tools(self) -> list[Tool[Any, Any]]:
        """本插件提供的工具实例（装载时注册，卸载时注销）。"""

    def setup(self) -> None:
        """初始化：建连接、校验配置。抛错即装载失败，注册表不会被污染。"""

    def teardown(self) -> None:
        """释放资源：卸载与装载回滚都会调用，必须可重复调用。"""
