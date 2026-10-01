"""SQLite 插件：把「查表」这个安全子集交给 Agent，而不是给它一把 SQL。

三条硬约束：
1. 库以 ``mode=ro`` 打开——插件在物理上写不了库，未来加错工具也不会写坏数据；
2. 表名/列名必须命中 introspect 出的真实 schema，其余一律拒绝（不拼接用户输入）；
3. 过滤值全部走占位符绑定，读写都不经过字符串拼接。
"""

from __future__ import annotations

import base64
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from driver_platform.errors import InvalidArgumentsError, InvalidConfigurationError
from driver_platform.plugins.base import Plugin
from driver_platform.tools.base import Tool

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SearchValue = str | int | float | bool | None


class SearchDatabaseInput(BaseModel):
    """结构化查询参数。没有 SQL 字段——这是本插件的全部安全边界。"""

    table: str = Field(min_length=1, max_length=64, description="表名，必须是库中已存在的表")
    columns: list[str] | None = Field(
        default=None, min_length=1, description="要返回的列；省略表示全部列"
    )
    filters: dict[str, SearchValue] | None = Field(
        default=None, description="等值过滤 {列名: 值}，多个条件之间是 AND"
    )
    limit: int = Field(default=100, ge=1, le=1000, description="本页最多返回的行数")
    offset: int = Field(default=0, ge=0, description="跳过的行数，用于翻页")


class SearchDatabaseOutput(BaseModel):
    table: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int = Field(description="rows 的长度")
    total: int = Field(description="符合过滤条件的总行数，不受 limit/offset 影响")
    truncated: bool = Field(description="是否还有没返回的行")


class SearchDatabaseTool(Tool[SearchDatabaseInput, SearchDatabaseOutput]):
    name = "search_database"
    contract_version = "1.0"
    description = "按表名做结构化查询：可投影列、可等值过滤、可分页；不执行任意 SQL"
    input_model = SearchDatabaseInput
    output_model = SearchDatabaseOutput

    def __init__(self, connection: sqlite3.Connection, lock: threading.Lock) -> None:
        self._connection = connection
        self._lock = lock

    async def execute(self, arguments: SearchDatabaseInput) -> SearchDatabaseOutput:
        # sqlite3 是阻塞 API，单连接上串行化，避免 "Recursive use of cursors"。
        with self._lock:
            return self._search(arguments)

    def _search(self, arguments: SearchDatabaseInput) -> SearchDatabaseOutput:
        tables = self._tables()
        if arguments.table not in tables:
            raise InvalidArgumentsError(
                f"表 {arguments.table} 不存在",
                details=[
                    {
                        "type": "unknown_table",
                        "loc": ["table"],
                        "msg": f"可用表：{', '.join(sorted(tables)) or '（库中没有任何表）'}",
                    }
                ],
            )

        available = self._columns(arguments.table)
        columns = list(arguments.columns) if arguments.columns else available
        unknown = [name for name in columns if name not in available]
        if unknown:
            raise InvalidArgumentsError(
                f"列不存在：{', '.join(unknown)}",
                details=[
                    {
                        "type": "unknown_column",
                        "loc": ["columns"],
                        "msg": f"表 {arguments.table} 的列：{', '.join(available)}",
                    }
                ],
            )

        filters = arguments.filters or {}
        unknown_filter_columns = [name for name in filters if name not in available]
        if unknown_filter_columns:
            raise InvalidArgumentsError(
                f"过滤条件里的列不存在：{', '.join(unknown_filter_columns)}",
                details=[
                    {
                        "type": "unknown_column",
                        "loc": ["filters"],
                        "msg": f"表 {arguments.table} 的列：{', '.join(available)}",
                    }
                ],
            )

        where_clause = ""
        parameters: list[Any] = []
        if filters:
            where_clause = " WHERE " + " AND ".join(
                f"{_quote_identifier(name)} = ?" for name in filters
            )
            parameters.extend(filters.values())

        table = _quote_identifier(arguments.table)
        select_clause = ", ".join(_quote_identifier(name) for name in columns)
        # bandit 的 B608 在这两处是误报：进入 SQL 文本的只有「已按真实 schema 校验过、
        # 且被 _quote_identifier 转义过的标识符」，所有值都由 ? 占位符绑定，用户输入进不了 SQL。
        select_sql = f"SELECT {select_clause} FROM {table}{where_clause} LIMIT ? OFFSET ?"  # nosec B608
        rows = self._connection.execute(
            select_sql,
            [*parameters, arguments.limit, arguments.offset],
        ).fetchall()
        count_sql = f"SELECT COUNT(*) FROM {table}{where_clause}"  # nosec B608
        total = self._connection.execute(
            count_sql,
            parameters,
        ).fetchone()[0]

        result_rows = [
            {name: _json_value(row[index]) for index, name in enumerate(columns)} for row in rows
        ]
        return SearchDatabaseOutput(
            table=arguments.table,
            columns=columns,
            rows=result_rows,
            row_count=len(result_rows),
            total=total,
            truncated=arguments.offset + len(result_rows) < total,
        )

    def _tables(self) -> set[str]:
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {row[0] for row in rows}

    def _columns(self, table: str) -> list[str]:
        # 每次调用读一次 schema：外部改了表结构，下一个请求就能看见，没有缓存要失效。
        rows = self._connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
        return [row[1] for row in rows]


class SqlitePlugin(Plugin):
    plugin_id = "sqlite"
    version = "0.1.0"
    description = "SQLite 数据库插件：提供只读的结构化查询工具 search_database"

    def __init__(self, path: str) -> None:
        self._path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._tool: SearchDatabaseTool | None = None

    def setup(self) -> None:
        if self._path != ":memory:":
            file_path = Path(self._path)
            if not file_path.is_file():
                raise InvalidConfigurationError(
                    f"DRIVER_SQLITE_PATH 指向的数据库文件不存在：{file_path.resolve()}"
                )
        try:
            connection = self._open()
            connection.execute("SELECT 1")
        except sqlite3.Error as exc:
            raise InvalidConfigurationError(
                f"无法以只读方式打开 SQLite 库 {self._path}：{exc}"
            ) from exc
        self._connection = connection
        self._tool = SearchDatabaseTool(connection, self._lock)

    def tools(self) -> list[Tool[Any, Any]]:
        return [self._tool] if self._tool is not None else []

    def teardown(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            self._tool = None

    def _open(self) -> sqlite3.Connection:
        if self._path == ":memory:":
            return sqlite3.connect(":memory:", check_same_thread=False)
        uri = f"{Path(self._path).resolve().as_uri()}?mode=ro"
        return sqlite3.connect(uri, uri=True, check_same_thread=False)


def _quote_identifier(name: str) -> str:
    """标识符一律双引号包裹并转义内部的引号（此处只用于已校验的表名/列名）。"""
    return '"' + name.replace('"', '""') + '"'


def _json_value(value: Any) -> Any:
    """BLOB 在 JSON 里没有原生类型，统一 base64。"""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value
