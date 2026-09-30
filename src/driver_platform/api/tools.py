"""工具相关接口：列出工具、执行工具。

统一入口 ``POST /v1/tools/execute`` 的请求体是 ``{tool, arguments}``，
与 MCP 的 ``tools/call``（``{name, arguments}``）同构：阶段 3 的 MCP 适配器
复用 ``tools.executor`` 的同一条执行管线，两边错误码不会分叉。

执行成功：``200 {"tool", "contract_version", "result"}``
执行失败：统一错误信封，``tool_not_found`` → 404、``invalid_arguments`` → 422、
``tool_execution_failed`` → 500。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from driver_platform.tools import ToolManifest, ToolRegistry, executor

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
    outcome = await executor.execute_tool(_registry(request), body.tool, body.arguments)
    return ExecuteResponse(
        tool=outcome.tool,
        contract_version=outcome.contract_version,
        result=outcome.result,
    )
