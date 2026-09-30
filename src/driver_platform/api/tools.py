"""工具相关接口：列出工具、执行工具。

统一入口 ``POST /v1/tools/execute`` 的请求体是 ``{tool, arguments}``，
与 MCP 的 ``tools/call``（``{name, arguments}``）同构，阶段 3 的适配只是改名映射。

执行成功：``200 {"tool", "contract_version", "result"}``
执行失败：统一错误信封，``tool_not_found`` → 404、``invalid_arguments`` → 422、
``tool_execution_failed`` → 500。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, ValidationError

from driver_platform.errors import (
    InvalidArgumentsError,
    PlatformError,
    ToolExecutionError,
    sanitize_validation_errors,
)
from driver_platform.tools import ToolManifest, ToolRegistry

router = APIRouter(prefix="/v1/tools", tags=["tools"])


class ExecuteRequest(BaseModel):
    tool: str = Field(min_length=1, description="要执行的工具名")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="工具入参，须满足该工具的 input_schema"
    )


class ExecuteResponse(BaseModel):
    tool: str
    contract_version: str
    result: Any


def _registry(request: Request) -> ToolRegistry:
    registry: ToolRegistry = request.app.state.registry
    return registry


@router.get("", response_model=list[ToolManifest], summary="列出已注册工具及其契约")
def list_tools(request: Request) -> list[ToolManifest]:
    return _registry(request).manifests()


@router.post("/execute", response_model=ExecuteResponse, summary="统一执行入口")
async def execute_tool(request: Request, body: ExecuteRequest) -> ExecuteResponse:
    tool = _registry(request).get(body.tool)

    try:
        arguments = tool.input_model.model_validate(body.arguments)
    except ValidationError as exc:
        raise InvalidArgumentsError(
            f"工具 {tool.name} 的入参不符合契约",
            details=sanitize_validation_errors(exc.errors()),
        ) from exc

    try:
        result = await tool.execute(arguments)
    except PlatformError:
        raise
    except Exception as exc:  # 插件内部错误 → 稳定 code，细节不外泄
        raise ToolExecutionError(f"工具 {tool.name} 执行失败") from exc

    if tool.output_model is not None:
        result = tool.output_model.model_validate(result).model_dump(mode="json")
    elif isinstance(result, BaseModel):
        result = result.model_dump(mode="json")

    return ExecuteResponse(
        tool=tool.name,
        contract_version=tool.contract_version,
        result=result,
    )
