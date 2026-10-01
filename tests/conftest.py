"""共享测试夹具。

两套客户端：``client`` 只装内置工具（阶段 1 的契约测试靠它），``db_client``
额外装 sqlite 插件——测试之间不共享进程环境变量，配置一律显式传入。

``remote_url`` 起一个**真的**远端插件服务（stdlib ``http.server``，线程里跑）：
远程插件的价值在于「真的跨进程说 HTTP」，用 mock 传输层测不出真 HTTP 路径
（``tests/test_remote_plugin.py`` 里另有 ``httpx.MockTransport`` 的单元测试，
那条线测的是错误映射与契约校验，不需要网络）。
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.main import create_app
from driver_platform.observability.context import request_id_var
from driver_platform.tools import ToolRegistry

REMOTE_PLUGIN_ID = "echo_farm"
REMOTE_PLUGIN_VERSION = "1.0.0"
REMOTE_TOOL_NAME = "remote_echo"

#: ``remote_url`` 那个假远端对外声明的清单（形状与 ``examples/remote_echo_service.py`` 一致）。
REMOTE_MANIFEST: dict[str, Any] = {
    "plugin_id": REMOTE_PLUGIN_ID,
    "version": REMOTE_PLUGIN_VERSION,
    "description": "测试用远端：把 message 回声回来",
    "tools": [
        {
            "name": REMOTE_TOOL_NAME,
            "contract_version": "1.0",
            "description": "回声工具",
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        }
    ],
}

DEMO_SCHEMA = """
CREATE TABLE drivers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT,
    status TEXT NOT NULL,
    rating REAL,
    avatar BLOB
);
"""

DEMO_ROWS = [
    (1, "张三", "上海", "active", 4.8, b"\x89PNG"),
    (2, "李四", "北京", "active", 4.2, None),
    (3, "王五", "上海", "inactive", 3.9, b"\x00\x01"),
    (4, "赵六", "深圳", "active", 4.5, None),
    (5, "钱七", "上海", "pending", None, None),
]


def _envelope(code: str, message: str) -> dict[str, Any]:
    """远端失败时回平台的错误信封（不是 FastAPI 的 ``{"detail": ...}``）。"""
    return {"error": {"code": code, "message": message, "details": None}}


class _RemotePluginHandler(BaseHTTPRequestHandler):
    """一个真说 HTTP 的远端插件：``GET /manifest`` + ``POST /execute``。

    行为对齐 ``examples/remote_echo_service.py``：远端**自己**校验入参（少给 ``message``
    就回 422 ``invalid_arguments``），好让测试覆盖「远端错误码原样透出」这条路径。
    """

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/manifest":
            self._reply(200, REMOTE_MANIFEST)
        else:
            self._reply(404, _envelope("tool_not_found", f"未知路径：{self.path}"))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            self._reply(400, _envelope("invalid_request", "请求体不是合法 JSON"))
            return
        if self.path != "/execute":
            self._reply(404, _envelope("tool_not_found", f"未知路径：{self.path}"))
            return
        assert isinstance(payload, dict)
        if payload.get("tool") != REMOTE_TOOL_NAME:
            self._reply(404, _envelope("tool_not_found", f"未提供的工具：{payload.get('tool')}"))
            return
        arguments = payload.get("arguments") or {}
        message = arguments.get("message") if isinstance(arguments, dict) else None
        if not isinstance(message, str) or not message:
            self._reply(422, _envelope("invalid_arguments", "message 必须是非空字符串"))
            return
        self._reply(
            200,
            {
                "tool": REMOTE_TOOL_NAME,
                "contract_version": "1.0",
                "result": {"echo": message, "arguments": arguments},
            },
        )

    def log_message(self, log_format: str, *args: Any) -> None:
        """静音访问日志：测试输出里不需要它。"""


@pytest.fixture
def remote_url() -> Iterator[str]:
    """起一个真的远端插件服务（随机端口），返回它的 ``base_url``。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RemotePluginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def dead_remote_url() -> Iterator[str]:
    """指向一个刚释放的端口：连上去必定连不上（用来测「远端不可达」）。

    实测（本机 Windows）：连一个没人监听的本地端口**不是立刻 RST 而是超时**，
    所以用它的用例都显式把 ``remote_timeout`` 调小，不让用例干等默认那 5 秒。
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    yield f"http://127.0.0.1:{port}"


@pytest.fixture
def app() -> FastAPI:
    return create_app(Settings())


@pytest.fixture
def registry(app: FastAPI) -> ToolRegistry:
    return app.state.registry


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_request_context() -> Iterator[None]:
    """把 ``request_id`` 的上下文清干净：``ContextVar`` 不会跨用例自动回滚。

    生产里不存在这个问题——每个请求在自己的任务上下文里跑；只有同进程连续跑用例时，
    上一个用例设过的号才会漏到下一个用例（那会让「没有请求上下文」的断言假阳性）。
    """
    token = request_id_var.set(None)
    yield
    request_id_var.reset(token)


@pytest.fixture
def demo_db(tmp_path: Path) -> Iterator[str]:
    """建一个真实 SQLite 文件库并关掉连接——插件是只读打开的。"""
    path = tmp_path / "demo.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(DEMO_SCHEMA)
        connection.executemany(
            "INSERT INTO drivers (id, name, city, status, rating, avatar) VALUES (?, ?, ?, ?, ?, ?)",
            DEMO_ROWS,
        )
        connection.commit()
    finally:
        connection.close()
    yield str(path)


@pytest.fixture
def db_client(demo_db: str) -> TestClient:
    return TestClient(create_app(Settings(plugins=("sqlite",), sqlite_path=demo_db)))


@pytest.fixture
def openai_client() -> TestClient:
    """只开 openai 协议、不装插件——协议开关与插件开关互不牵连。"""
    return TestClient(create_app(Settings(protocols=("openai",))))


@pytest.fixture
def openai_db_client(demo_db: str) -> TestClient:
    """协议 + 插件都开：验收要求 OpenAI Schema 里能看到 search_database。"""
    return TestClient(
        create_app(Settings(plugins=("sqlite",), sqlite_path=demo_db, protocols=("openai",)))
    )


@pytest.fixture
def sqlite_lib(tmp_path: Path) -> Iterator[str]:
    """空库（只有 sqlite_master，没有任何业务表）。"""
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    yield str(path)
