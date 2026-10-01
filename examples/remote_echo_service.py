"""一个最小的远程插件服务：把远端契约（``docs/stages/stage-6.md``）跑给人看。

远端要做的只有两件事：报清单、执行工具。

    # 1) 起远端（另开一个终端）
    .venv\\Scripts\\python.exe examples/remote_echo_service.py --port 9100

    # 2) 平台侧开管理接口 + 带管理作用域的钥匙
    $env:DRIVER_REMOTE_PLUGIN_ENABLED = "1"
    $env:DRIVER_AUTH_MODE = "required"
    $env:DRIVER_API_KEYS = "admin-key:plugin_admin"
    .venv\\Scripts\\python.exe -m uvicorn driver_platform.main:app --port 8000

    # 3) 注册、调用、下线
    curl.exe -X POST http://127.0.0.1:8000/v1/plugins/remote \
      -H "X-API-Key: admin-key" -H "Content-Type: application/json" \
      -d '{"plugin_id":"echo_farm","base_url":"http://127.0.0.1:9100"}'
    curl.exe -X POST http://127.0.0.1:8000/v1/tools/execute \
      -H "Content-Type: application/json" \
      -d '{"tool":"remote_echo","arguments":{"message":"你好","times":2}}'
    curl.exe -X DELETE http://127.0.0.1:8000/v1/plugins/remote/echo_farm -H "X-API-Key: admin-key"

注意两点：远端**自己**校验入参（平台不替它校验），失败时回我们的错误信封——
``remote_echo`` 少给 ``message`` 就会看到 ``invalid_arguments`` 422。
"""

from __future__ import annotations

import argparse
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

PLUGIN_ID = "echo_farm"
PLUGIN_VERSION = "1.0.0"
TOOL_NAME = "remote_echo"
TOOL_CONTRACT_VERSION = "1.0"

MANIFEST: dict[str, Any] = {
    "plugin_id": PLUGIN_ID,
    "version": PLUGIN_VERSION,
    "description": "示例远程插件：把入参原样回声回来",
    "tools": [
        {
            "name": TOOL_NAME,
            "contract_version": TOOL_CONTRACT_VERSION,
            "description": "回声工具：times 可以要求重复几次（1–10）",
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "times": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        }
    ],
}

app = FastAPI(title="远程插件示例（echo_farm）")


class ExecuteRequest(BaseModel):
    tool: str
    arguments: dict[str, Any]


@app.get("/manifest", summary="报告本远端的插件身份与工具清单")
def manifest() -> dict[str, Any]:
    return MANIFEST


@app.post("/execute", summary="执行一个工具")
def execute(payload: ExecuteRequest) -> JSONResponse:
    if payload.tool != TOOL_NAME:
        return _error(404, "tool_not_found", f"未提供的工具：{payload.tool}")
    message = payload.arguments.get("message")
    if not isinstance(message, str) or not message:
        return _error(422, "invalid_arguments", "message 必须是非空字符串")
    times = payload.arguments.get("times", 1)
    if not isinstance(times, int) or isinstance(times, bool) or not 1 <= times <= 10:
        return _error(422, "invalid_arguments", "times 必须是 1–10 的整数")
    return JSONResponse(
        {
            "tool": TOOL_NAME,
            "contract_version": TOOL_CONTRACT_VERSION,
            "result": {"echo": message, "times": times, "repeated": [message] * times},
        }
    )


def _error(status: int, code: str, message: str) -> JSONResponse:
    """错误一律用平台的错误信封：调用方不需要为远端再学一套形状。"""
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "details": None}}
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="远程插件示例服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
