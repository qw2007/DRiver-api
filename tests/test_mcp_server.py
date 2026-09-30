"""MCP stdio 端到端：用官方客户端 spawn 真实进程，走完 initialize / list / call。

这正是阶段 3 的验收条件（「MCP 客户端能 list/call ``search_database``」）：
不用自造的假客户端，因为假客户端只能验证「我实现的东西符合我以为的协议」。

mcp 是可选依赖：没装整文件跳过；装了就必须过。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

MODULE = "driver_platform.protocols.mcp_server"


def _env(**overrides: str) -> dict[str, str]:
    """子进程环境 = 当前环境 + 覆写；显式传 env 时 SDK 不会自动补默认环境。"""
    return {**os.environ, **overrides}


async def _observe(env: dict[str, str]) -> dict[str, Any]:
    """一个子进程里跑完所有调用，省掉重复启动的开销。"""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", MODULE], env=env)
    # 不传 errlog：SDK 把它当子进程的 stderr 用，需要真实 fileno（StringIO 会 UnsupportedOperation）
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        init = await session.initialize()
        listed = await session.list_tools()
        return {
            "server": init.serverInfo,
            "capabilities_tools": init.capabilities.tools,
            "tools": {tool.name: tool for tool in listed.tools},
            "echo": await session.call_tool("echo", {"message": "hi", "times": 2}),
            "search": await session.call_tool("search_database", {"table": "drivers", "limit": 1}),
            "unknown_tool": await session.call_tool("ghost", {}),
            "bad_arguments": await session.call_tool("echo", {}),
            "unknown_table": await session.call_tool("search_database", {"table": "ghosts"}),
        }


def test_stdio_server_lists_and_calls_tools(demo_db: str) -> None:
    seen = asyncio.run(
        _observe(_env(DRIVER_PROTOCOLS="mcp", DRIVER_PLUGINS="sqlite", DRIVER_SQLITE_PATH=demo_db))
    )

    assert seen["server"].name == "driver-platform"
    assert seen["capabilities_tools"] is not None  # 声明了 tools 能力，客户端才敢调

    assert set(seen["tools"]) == {"echo", "search_database"}
    search_tool = seen["tools"]["search_database"]
    assert set(search_tool.inputSchema["properties"]) == {
        "table",
        "columns",
        "filters",
        "limit",
        "offset",
    }
    assert search_tool.outputSchema is not None  # 输出契约也交给协议层兜底

    echo = seen["echo"]
    assert echo.isError is False
    assert echo.structuredContent == {"echo": "hi\nhi", "times": 2}

    search = seen["search"]
    assert search.isError is False
    assert search.structuredContent["table"] == "drivers"
    assert search.structuredContent["row_count"] == 1
    assert search.structuredContent["total"] == 5  # 分页前的总数
    assert search.structuredContent["truncated"] is True

    # 工具级失败是**结果**（isError），不是 JSON-RPC 错误：模型能看见平台错误码
    assert seen["unknown_tool"].isError is True
    assert seen["unknown_tool"].structuredContent["error"]["code"] == "tool_not_found"
    assert seen["bad_arguments"].isError is True
    assert seen["bad_arguments"].structuredContent["error"]["code"] == "invalid_arguments"
    # 中文错误信息必须在 stdio 管道上原样往返（Windows 默认编码不是 UTF-8，这里钉死）
    assert "不存在" in seen["unknown_table"].structuredContent["error"]["message"]


def test_stdio_entry_refuses_to_start_without_the_switch() -> None:
    """开关默认关：没在 DRIVER_PROTOCOLS 里开 mcp，进程必须直接失败。"""
    completed = subprocess.run(
        [sys.executable, "-m", MODULE],
        env=_env(DRIVER_PROTOCOLS=""),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 2
    assert "protocol_not_enabled" in completed.stderr


def test_stdio_entry_explains_a_missing_optional_dependency(tmp_path: Path) -> None:
    """没装可选依赖时给人话，不给 traceback。

    用一个假的 ``mcp`` 包（PYTHONPATH 比 site-packages 先命中）模拟「没装」，
    而不是真去卸载依赖——CI 里 dev extra 一定装着 mcp。
    """
    stub = tmp_path / "stub" / "mcp"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("raise ImportError('假装没装 mcp')\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", MODULE],
        env=_env(DRIVER_PROTOCOLS="mcp", PYTHONPATH=str(stub.parent)),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 2
    assert "driver-platform[mcp]" in completed.stderr
    assert "Traceback" not in completed.stderr
