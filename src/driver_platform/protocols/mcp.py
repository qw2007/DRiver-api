"""MCP 适配器：把工具注册表挂成一个 MCP stdio 服务端。

本模块是**唯一** import ``mcp`` SDK 的地方（``protocols/mcp_server.py`` 是它的进程入口），
``tests/test_protocol_isolation.py`` 用 AST 扫 import 把这条边界钉成了测试。

三条写下来的约定：

1. **工具级失败不是协议错误**。MCP 的惯例是回 ``CallToolResult(isError=True)``，
   模型能看见失败原因并自我纠正；只有 JSON-RPC 层的问题才是 ``-32xxx``。
   这里的错误信封与 HTTP 侧**逐字相同**（同一个 ``PlatformError.to_payload()``）。
2. **入参校验交给平台自己的执行管线**（``@server.call_tool(validate_input=False)``）。
   SDK 默认会先用 ``inputSchema`` 做一遍 jsonschema 校验，失败时只回一句英文
   ``Input validation error: ...``——那样 MCP 与 HTTP 就会报出两套错误，
   所以这里关掉 SDK 的校验，让 ``tools.executor`` 报出同一个 ``invalid_arguments``。
3. **作用域在适配器里只做「翻译」**（阶段 4）：凭证 → ``Principal`` 的逻辑在
   ``security.policy``，与 HTTP 共用；这里只把可见工具翻译成 ``types.Tool``、
   把无权访问翻译成 ``isError=True`` 的结果。
"""

from __future__ import annotations

import json
from typing import Any

from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from driver_platform import __version__
from driver_platform.errors import PlatformError
from driver_platform.security import ANONYMOUS, Principal, visible_manifests
from driver_platform.tools.base import ToolManifest
from driver_platform.tools.executor import execute_tool
from driver_platform.tools.registry import ToolRegistry

SERVER_NAME = "driver-platform"
"""MCP ``serverInfo.name``：客户端界面上显示的服务名。"""

__all__ = ["SERVER_NAME", "build_server", "error_result", "serve", "success_result", "to_mcp_tool"]


def to_mcp_tool(manifest: ToolManifest) -> types.Tool:
    """``ToolManifest`` → MCP ``Tool``。

    ``outputSchema`` 有值就带上：客户端会用它校验 ``structuredContent``，
    等于把「输出契约」也交给协议层兜底。
    """
    return types.Tool(
        name=manifest.name,
        title=manifest.name,
        description=manifest.description or None,
        inputSchema=manifest.input_schema,
        outputSchema=manifest.output_schema,
    )


def _text(payload: Any) -> types.TextContent:
    """给模型看的文本形式：缩进过的 JSON，人和模型都能直接读。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return types.TextContent(type="text", text=text)


def success_result(result: Any) -> types.CallToolResult:
    """成功结果。

    ``structuredContent`` 只接受对象（MCP 规范），所以工具没有 ``output_model`` 时
    （``result`` 可能是 ``None``）只给文本，不给结构化字段。
    """
    return types.CallToolResult(
        content=[_text(result)],
        structuredContent=result if isinstance(result, dict) else None,
    )


def error_result(exc: PlatformError) -> types.CallToolResult:
    """工具级失败：``isError=True`` + 与 HTTP 完全一致的信封。"""
    payload = exc.to_payload()
    return types.CallToolResult(content=[_text(payload)], structuredContent=payload, isError=True)


def build_server(registry: ToolRegistry, principal: Principal = ANONYMOUS) -> Server:
    """把注册表包成一个 MCP ``Server``（不做任何 I/O，便于单测与复用）。

    ``principal`` 是这次 stdio 会话的身份——进程由 Agent spawn，凭证只能来自
    它给的环境变量（``DRIVER_API_KEY``），所以一进程一身份，不需要按调用解析。
    """
    server: Server = Server(SERVER_NAME, version=__version__)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            to_mcp_tool(manifest) for manifest in visible_manifests(registry.manifests(), principal)
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            outcome = await execute_tool(registry, name, arguments, principal=principal)
        except PlatformError as exc:
            return error_result(exc)
        return success_result(outcome.result)

    return server


async def serve(registry: ToolRegistry, principal: Principal = ANONYMOUS) -> None:
    """在 stdin/stdout 上服务，直到客户端断开。"""
    server = build_server(registry, principal)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
