"""工具执行管线：校验 → 执行 → 序列化。

HTTP 的 ``POST /v1/tools/execute`` 与 MCP 的 ``tools/call`` 共用这一条路径。
两条协议必须在**同一处**成功、也在**同一处**失败：MCP 适配器若另写一份
``try/except``，错误码迟早分叉，Agent 换个协议就换一套行为，中间层的价值就没了。

失败一律是 ``PlatformError``：``tool_not_found`` / ``invalid_arguments`` /
``tool_execution_failed``——调用方不需要认识任何其他异常类型。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from driver_platform.errors import (
    InvalidArgumentsError,
    PlatformError,
    ToolExecutionError,
    sanitize_validation_errors,
)
from driver_platform.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """一次成功执行：工具身份 + JSON 安全的 ``result``。"""

    tool: str
    contract_version: str
    result: Any


async def execute_tool(
    registry: ToolRegistry, name: str, arguments: Mapping[str, Any]
) -> ExecutionResult:
    """按名字执行工具。

    ``arguments`` 是原始字典（HTTP 请求体或 MCP 的 ``arguments``），
    这里负责用 ``input_model`` 校验它——协议适配器不做第二套校验。
    """
    tool = registry.get(name)

    try:
        validated = tool.input_model.model_validate(arguments)
    except ValidationError as exc:
        raise InvalidArgumentsError(
            f"工具 {tool.name} 的入参不符合契约",
            details=sanitize_validation_errors(exc.errors()),
        ) from exc

    try:
        result = await tool.execute(validated)
    except PlatformError:
        raise
    except Exception as exc:  # 插件内部错误 → 稳定 code，细节不外泄
        raise ToolExecutionError(f"工具 {tool.name} 执行失败") from exc

    if tool.output_model is not None:
        result = tool.output_model.model_validate(result).model_dump(mode="json")
    elif isinstance(result, BaseModel):
        result = result.model_dump(mode="json")

    return ExecutionResult(
        tool=tool.name,
        contract_version=tool.contract_version,
        result=result,
    )
