"""阶段 4 的 MCP 一侧：作用域必须在 ``tools/list`` 与 ``tools/call`` 两处都生效。

本文件 spawn **真实子进程**：stdio 传输没有 HTTP 头可放凭证，Agent 只能通过进程
环境变量把钥匙交给它（``DRIVER_API_KEY``，单数）。所以这些用例同时钉住了
「MCP 的凭证来源」这条设计——服务端清单（复数）与客户端自己的钥匙（单数）分开。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import Any

import pytest

pytest.importorskip("mcp")

MODULE = "driver_platform.protocols.mcp_server"
FULL_KEY = "key-full-0001"
ECHO_KEY = "key-echo-0002"
KEYS = f"{FULL_KEY}, {ECHO_KEY}:echo"


def _env(**overrides: str) -> dict[str, str]:
    """子进程环境 = 当前环境 + 覆写；显式传 env 时 SDK 不会自动补默认环境。"""
    return {**os.environ, **overrides}


def _server_env(demo_db: str | None, **extra: str) -> dict[str, str]:
    overrides: dict[str, str] = {
        "DRIVER_PROTOCOLS": "mcp",
        "DRIVER_AUTH_MODE": "required",
        "DRIVER_API_KEYS": KEYS,
        **extra,
    }
    if demo_db is not None:
        overrides |= {"DRIVER_PLUGINS": "sqlite", "DRIVER_SQLITE_PATH": demo_db}
    return _env(**overrides)


async def _observe(env: dict[str, str]) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", MODULE], env=env)
    # 不传 errlog：SDK 把它当子进程的 stderr 用，需要真实 fileno（StringIO 会 UnsupportedOperation）
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        return {
            "tools": sorted(tool.name for tool in listed.tools),
            "echo": await session.call_tool("echo", {"message": "hi"}),
            "search": await session.call_tool("search_database", {"table": "drivers"}),
        }


def test_scoped_key_only_sees_and_calls_its_own_tool(demo_db: str) -> None:
    seen = asyncio.run(_observe(_server_env(demo_db, DRIVER_API_KEY=ECHO_KEY)))

    assert seen["tools"] == ["echo"]  # 不可见即不可用
    assert seen["echo"].isError is False
    assert seen["search"].isError is True  # 越权是**结果**，不是 JSON-RPC 错误
    assert seen["search"].structuredContent["error"]["code"] == "permission_denied"


def test_full_key_sees_everything(demo_db: str) -> None:
    seen = asyncio.run(_observe(_server_env(demo_db, DRIVER_API_KEY=FULL_KEY)))

    assert seen["tools"] == ["echo", "search_database"]
    assert seen["search"].isError is False


def test_startup_fails_without_the_key_when_required() -> None:
    """鉴权已开、Agent 没给钥匙：进程直接失败，而不是以匿名身份服务。"""
    env = _server_env(None)
    env.pop("DRIVER_API_KEY", None)

    completed = subprocess.run(
        [sys.executable, "-m", MODULE],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 2
    assert "missing_credentials" in completed.stderr
