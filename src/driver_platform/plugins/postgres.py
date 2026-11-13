"""PostgreSQL 插件：与 SQLite 插件同一套契约，只是换了个库。

刻意复用 ``SearchDatabaseInput`` / ``SearchDatabaseOutput``：Agent 看到的结构完全一样
（表名 + 列投影 + 等值过滤 + 分页，没有 SQL 字段），换库不该改变使用方式。区别只在
实现——SQLite 用 ``mode=ro`` 打开文件，PostgreSQL 用会话级的
``default_transaction_read_only=on``，两者都是「插件在物理上写不了库」。

可选依赖：``pip install "driver-platform[postgres]"``（psycopg 3）。没装时插件仍然
可以被注册，只是 ``setup()`` 会给出明确的安装提示，而不是在 import 期炸掉整个平台。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from driver_platform.errors import InvalidArgumentsError, InvalidConfigurationError
from driver_platform.plugins.base import Plugin
from driver_platform.plugins.sqlite import (
    SearchDatabaseInput,
    SearchDatabaseOutput,
    _json_value,
)
from driver_platform.tools.base import Tool

#: 系统 schema 不暴露给 Agent：它们能看但会把「可用表」的提示淹没。
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")

#: 建连超时。不设它时 psycopg 会一直等——库不在线时插件装载会挂住整个平台启动。
CONNECT_TIMEOUT = 5


class SearchPostgresTool(Tool[SearchDatabaseInput, SearchDatabaseOutput]):
    """按表名做结构化查询；SQL 文本里只出现已校验的标识符。"""

    name = "search_postgres"
    contract_version = "1.0"
    description = "在 PostgreSQL 上按表名做结构化查询：可投影列、可等值过滤、可分页；不执行任意 SQL"
    input_model = SearchDatabaseInput
    output_model = SearchDatabaseOutput

    def __init__(self, connection: Any, lock: threading.Lock) -> None:
        self._connection = connection
        self._lock = lock

    async def execute(self, arguments: SearchDatabaseInput) -> SearchDatabaseOutput:
        # psycopg 是阻塞 API；丢到线程里跑，别占住事件循环。
        return await asyncio.to_thread(self._search, arguments)

    def _search(self, arguments: SearchDatabaseInput) -> SearchDatabaseOutput:
        with self._lock:
            return self._query(arguments)

    def _query(self, arguments: SearchDatabaseInput) -> SearchDatabaseOutput:
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
                f"{_quote_identifier(name)} = %s" for name in filters
            )
            parameters.extend(filters.values())

        table = _quote_identifier(arguments.table)
        select_clause = ", ".join(_quote_identifier(name) for name in columns)
        # B608 误报同 sqlite.py：SQL 文本里只有校验过的标识符，值全部走 %s 绑定。
        select_sql = f"SELECT {select_clause} FROM {table}{where_clause} LIMIT %s OFFSET %s"  # nosec B608
        with self._connection.cursor() as cursor:
            cursor.execute(select_sql, [*parameters, arguments.limit, arguments.offset])
            rows = cursor.fetchall()
            count_sql = f"SELECT COUNT(*) FROM {table}{where_clause}"  # nosec B608
            cursor.execute(count_sql, parameters)
            total = cursor.fetchone()[0]

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
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN (%s, %s) AND table_type = 'BASE TABLE'",
                list(_SYSTEM_SCHEMAS),
            )
            return {row[0] for row in cursor.fetchall()}

    def _columns(self, table: str) -> list[str]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND table_schema NOT IN (%s, %s) "
                "ORDER BY ordinal_position",
                [table, *_SYSTEM_SCHEMAS],
            )
            return [row[0] for row in cursor.fetchall()]


class PostgresPlugin(Plugin):
    plugin_id = "postgres"
    version = "0.1.0"
    description = "PostgreSQL 数据库插件：提供只读的结构化查询工具 search_postgres"

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._connection: Any = None
        self._lock = threading.Lock()
        self._tool: SearchPostgresTool | None = None

    def setup(self) -> None:
        psycopg = _import_psycopg()
        try:
            # 会话级只读：即使工具层出 bug 拼出写语句，库也会拒。
            connection = psycopg.connect(
                self._dsn,
                autocommit=True,
                connect_timeout=CONNECT_TIMEOUT,
                options="-c default_transaction_read_only=on",
            )
            connection.execute("SELECT 1")
        except Exception as exc:  # psycopg 的异常类型随版本变化，统一归类
            raise InvalidConfigurationError(
                f"无法以只读方式连接 PostgreSQL {_safe_dsn(self._dsn)}：{exc}"
            ) from exc
        self._connection = connection
        self._tool = SearchPostgresTool(connection, self._lock)

    def tools(self) -> list[Tool[Any, Any]]:
        return [self._tool] if self._tool is not None else []

    def teardown(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            self._tool = None


def _import_psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 取决于环境是否装了可选依赖
        raise InvalidConfigurationError(
            "PostgreSQL 插件需要可选依赖：pip install 'driver-platform[postgres]'"
        ) from exc
    return psycopg


def _safe_dsn(dsn: str) -> str:
    """错误信息里只回主机与库名，避免把密码写进日志。"""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "<无法解析的 DSN>"
    host = parts.hostname or "?"
    database = parts.path.lstrip("/") or "?"
    return f"{host}/{database}"


def _quote_identifier(name: str) -> str:
    """标识符一律双引号包裹并转义内部引号（只用于已按真实 schema 校验过的名字）。"""
    return '"' + name.replace('"', '""') + '"'


__all__ = ["PostgresPlugin", "SearchPostgresTool"]
