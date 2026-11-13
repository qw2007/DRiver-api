"""PostgreSQL 插件：契约、错误路径，以及（有库时才跑的）真实查询。"""

from __future__ import annotations

import asyncio
import os

import pytest

from driver_platform.config import Settings
from driver_platform.errors import InvalidArgumentsError, InvalidConfigurationError
from driver_platform.plugins.catalog import build_plugin
from driver_platform.plugins.postgres import PostgresPlugin, _safe_dsn
from driver_platform.plugins.sqlite import SearchDatabaseInput
from driver_platform.tools.executor import execute_tool
from driver_platform.tools.registry import ToolRegistry

DSN_ENV = "DRIVER_TEST_POSTGRES_DSN"


def test_factory_requires_a_dsn() -> None:
    with pytest.raises(InvalidConfigurationError) as excinfo:
        build_plugin("postgres", Settings())
    assert "DRIVER_POSTGRES_DSN" in str(excinfo.value)


def test_factory_builds_a_plugin() -> None:
    plugin = build_plugin("postgres", Settings(postgres_dsn="postgresql://demo@localhost/shop"))
    assert isinstance(plugin, PostgresPlugin)
    assert plugin.plugin_id == "postgres"


def test_dsn_is_reduced_to_host_and_database() -> None:
    # 错误信息里不能出现密码——它会被写进日志与响应。
    assert _safe_dsn("postgresql://demo:secret@db.internal:5432/shop") == "db.internal/shop"


def test_unreachable_database_is_a_configuration_error() -> None:
    plugin = PostgresPlugin("postgresql://demo:secret@127.0.0.1:1/shop")
    with pytest.raises(InvalidConfigurationError) as excinfo:
        plugin.setup()
    assert "secret" not in str(excinfo.value)


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    dsn = os.environ.get(DSN_ENV, "")
    if not dsn:
        pytest.skip(f"未设置 {DSN_ENV}（本机起一个 postgres 容器即可跑这组用例）")
    return dsn


@pytest.fixture(scope="module")
def pg_client(pg_dsn: str) -> object:
    import psycopg

    connection = psycopg.connect(pg_dsn, autocommit=True)
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS customers (id serial PRIMARY KEY, name text, city text)"
        )
        cursor.execute("TRUNCATE customers RESTART IDENTITY")
        cursor.executemany(
            "INSERT INTO customers (name, city) VALUES (%s, %s)",
            [("Acme 制造", "上海"), ("北斗数据", "北京"), ("海纳咨询", "上海")],
        )
    connection.close()
    plugin = PostgresPlugin(pg_dsn)
    plugin.setup()
    yield plugin
    plugin.teardown()


def test_real_query_returns_rows_and_filters(pg_client: PostgresPlugin) -> None:
    registry = ToolRegistry()
    registry.register(pg_client.tools()[0])

    async def run() -> dict[str, object]:
        arguments = SearchDatabaseInput(table="customers", filters={"city": "上海"}, limit=2)
        result = await execute_tool(registry, "search_postgres", arguments.model_dump())
        return dict(result.result)  # type: ignore[arg-type]

    result = asyncio.run(run())
    assert result["table"] == "customers"
    assert result["columns"] == ["id", "name", "city"]
    assert [row["name"] for row in result["rows"]] == ["Acme 制造", "海纳咨询"]  # type: ignore[index]
    assert result["total"] == 2
    assert result["truncated"] is False


def test_unknown_table_and_column_are_rejected(pg_client: PostgresPlugin) -> None:
    tool = pg_client.tools()[0]
    with pytest.raises(InvalidArgumentsError) as unknown_table:
        asyncio.run(tool.execute(SearchDatabaseInput(table="nope")))
    assert "不存在" in str(unknown_table.value)

    with pytest.raises(InvalidArgumentsError) as unknown_column:
        asyncio.run(tool.execute(SearchDatabaseInput(table="customers", columns=["nope"])))
    assert "列不存在" in str(unknown_column.value)


def test_system_schemas_are_not_advertised(pg_client: PostgresPlugin) -> None:
    tool = pg_client.tools()[0]
    tables = tool._tables()
    assert "customers" in tables
    assert "pg_class" not in tables
    assert "information_schema" not in {name.split(".")[0] for name in tables}
