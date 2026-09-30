"""工具相关接口：列出工具、执行工具。

统一入口 ``POST /v1/tools/execute`` 的请求体是 ``{tool, arguments}``，
与 MCP 的 ``tools/call``（``{name, arguments}``）同构：阶段 3 的 MCP 适配器
复用 ``tools.executor`` 的同一条执行管线，两边错误码不会分叉。

阶段 4 起两个端点都要凭证（``PrincipalDep``）：列表按作用域过滤，
执行在管线上判权。**没有中间件**——``/health`` 必须保持公开，而作用域判断
也得先知道这次要调哪个工具。

成功：``200 {"tool", "contract_version", "result"}``
失败：统一错误信封——``missing_credentials`` / ``invalid_credentials`` → 401、
``permission_denied`` → 403、``tool_not_found`` → 404、``invalid_arguments`` → 422、
``tool_execution_failed`` → 500。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from driver_platform.security import PrincipalDep, visible_manifests
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
def list_tools(request: Request, principal: PrincipalDep) -> list[ToolManifest]:
    """只列**该凭证能用**的工具：能力不可见即不可用。"""
    return visible_manifests(_registry(request).manifests(), principal)


@router.post("/execute", response_model=ExecuteResponse, summary="统一执行入口")
async def execute_tool(
    request: Request,
    body: ExecuteRequest,
    principal: PrincipalDep,
) -> ExecuteResponse:
    outcome = await executor.execute_tool(
        _registry(request), body.tool, body.arguments, principal=principal
    )
    return ExecuteResponse(
        tool=outcome.tool,
        contract_version=outcome.contract_version,
        result=outcome.result,
    )
