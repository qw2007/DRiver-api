"""统一错误契约。

原则「接口先行」：任何失败响应都必须是同一个形状，调用方（Agent / MCP 客户端）
只按 ``error.code`` 分支，不解析自然语言 ``message``，也不依赖 HTTP 状态码之外的信息。

    {"error": {"code": "tool_not_found", "message": "...", "details": null}}

``code`` 是稳定标识，属于对外契约；新增 code 算兼容变更，改名必须升 ``contract_version`` 主版本。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class PlatformError(Exception):
    """平台内所有**预期**失败的基类。未预期异常不冒充这类错误。"""

    code = "internal_error"
    status_code = 500

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class InvalidRequestError(PlatformError):
    """请求体不符合接口约定（字段缺失、类型错误）。"""

    code = "invalid_request"
    status_code = 422


class ToolNotFoundError(PlatformError):
    """请求的工具名没有注册。"""

    code = "tool_not_found"
    status_code = 404


class InvalidArgumentsError(PlatformError):
    """工具存在，但 arguments 不满足该工具的 input_schema。"""

    code = "invalid_arguments"
    status_code = 422


class InvalidToolError(PlatformError):
    """插件作者写错了契约（工具名/版本号非法），属于启动期错误。"""

    code = "invalid_tool"
    status_code = 500


class DuplicateToolError(PlatformError):
    """同一工具名被注册两次。后者被拒绝，避免静默覆盖。"""

    code = "duplicate_tool"
    status_code = 500


class ToolExecutionError(PlatformError):
    """工具内部抛错。对外只暴露一个稳定的 code，细节留在服务端日志。"""

    code = "tool_execution_failed"
    status_code = 500


def sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剥掉 pydantic 报错里的 ``ctx`` / ``input`` / ``url``。

    这些字段可能含异常对象（不可 JSON 序列化）或回显用户原文，只保留
    ``type`` / ``loc`` / ``msg`` 这三个定位字段。
    """
    return [
        {key: value for key, value in error.items() if key in {"type", "loc", "msg"}}
        for error in errors
    ]


def register_error_handlers(app: FastAPI) -> None:
    """把平台错误与 FastAPI 自带校验错误统一到同一个信封里。"""

    @app.exception_handler(PlatformError)
    async def _handle_platform_error(_: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        error = InvalidRequestError(
            "请求体不符合接口约定", details=sanitize_validation_errors(exc.errors())
        )
        return JSONResponse(status_code=error.status_code, content=error.to_payload())
