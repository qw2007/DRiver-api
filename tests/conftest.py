"""共享测试夹具。

两套客户端：``client`` 只装内置工具（阶段 1 的契约测试靠它），``db_client``
额外装 sqlite 插件——测试之间不共享进程环境变量，配置一律显式传入。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.main import create_app
from driver_platform.observability.context import request_id_var
from driver_platform.tools import ToolRegistry

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
