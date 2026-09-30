"""工具契约：插件要实现什么，注册表对外承诺什么。

原则「插件契约版本化」：``contract_version`` 形如 ``MAJOR.MINOR``。
破坏性变更（改入参字段、改 code、删字段）必须升 MAJOR；只加可选字段升 MINOR。
调用方按 MAJOR 分支，平台不替插件做兼容层。

工具名与版本号在 ``register()`` 时校验，写错契约在启动期就炸，不留到线上。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CONTRACT_VERSION_PATTERN = re.compile(r"^\d+\.\d+$")

InT = TypeVar("InT", bound=BaseModel)
OutT = TypeVar("OutT", bound=BaseModel)


class ToolManifest(BaseModel):
    """工具的对外说明书。阶段 3 的 MCP / OpenAI Schema 适配直接吃这份结构。"""

    name: str
    contract_version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class Tool(ABC, Generic[InT, OutT]):
    """插件基类：声明元数据 + 实现 ``execute``。

    子类只需给出类属性与 ``execute``，入参校验、错误包装、响应封装由平台负责：
    ``execute`` 拿到的永远是已经通过 ``input_model`` 校验的对象。
    """

    name: str
    contract_version: str = "1.0"
    description: str = ""
    input_model: type[InT]
    output_model: type[OutT] | None = None

    @classmethod
    def manifest(cls) -> ToolManifest:
        return ToolManifest(
            name=cls.name,
            contract_version=cls.contract_version,
            description=cls.description,
            input_schema=cls.input_model.model_json_schema(),
            output_schema=(
                cls.output_model.model_json_schema() if cls.output_model is not None else None
            ),
        )

    @abstractmethod
    async def execute(self, arguments: InT) -> OutT:
        """执行工具。抛出的异常由平台统一转成 ``tool_execution_failed``。"""
