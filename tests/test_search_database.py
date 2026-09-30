"""search_database 的行为测试：结构化查询能做什么、越界时怎么拒绝。"""

from __future__ import annotations

import base64
import sqlite3

import pytest
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.main import create_app


def call(client: TestClient, **arguments) -> dict:
    response = client.post(
        "/v1/tools/execute", json={"tool": "search_database", "arguments": arguments}
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_plugin_registers_search_database(db_client: TestClient):
    response = db_client.get("/v1/plugins")

    assert response.status_code == 200
    assert response.json() == [
        {
            "plugin_id": "sqlite",
            "version": "0.1.0",
            "description": "SQLite 数据库插件：提供只读的结构化查询工具 search_database",
            "tools": [{"name": "search_database", "contract_version": "1.0"}],
        }
    ]


def test_tool_manifest_is_visible_in_tool_list(db_client: TestClient):
    body = db_client.get("/v1/tools").json()

    assert [item["name"] for item in body] == ["echo", "search_database"]
    schema = body[1]["input_schema"]
    assert schema["required"] == ["table"]
    assert set(schema["properties"]) == {"table", "columns", "filters", "limit", "offset"}
    assert schema["properties"]["limit"]["maximum"] == 1000


def test_search_returns_all_columns_and_counts(db_client: TestClient):
    result = call(db_client, table="drivers")

    assert result["columns"] == ["id", "name", "city", "status", "rating", "avatar"]
    assert result["row_count"] == 5
    assert result["total"] == 5
    assert result["truncated"] is False
    assert result["rows"][0]["name"] == "张三"


def test_column_projection(db_client: TestClient):
    result = call(db_client, table="drivers", columns=["id", "name"])

    assert result["columns"] == ["id", "name"]
    assert set(result["rows"][0]) == {"id", "name"}


def test_equality_filter_reports_filtered_total(db_client: TestClient):
    result = call(db_client, table="drivers", filters={"status": "active"})

    assert result["total"] == 3
    assert sorted(row["id"] for row in result["rows"]) == [1, 2, 4]


def test_multiple_filters_are_combined_with_and(db_client: TestClient):
    result = call(db_client, table="drivers", filters={"city": "上海", "status": "active"})

    assert result["total"] == 1
    assert result["rows"][0]["id"] == 1


def test_pagination_reports_truncation(db_client: TestClient):
    first = call(db_client, table="drivers", limit=2)
    second = call(db_client, table="drivers", limit=2, offset=2)
    last = call(db_client, table="drivers", limit=2, offset=4)

    assert (first["row_count"], first["total"], first["truncated"]) == (2, 5, True)
    assert (second["row_count"], second["total"], second["truncated"]) == (2, 5, True)
    assert (last["row_count"], last["total"], last["truncated"]) == (1, 5, False)


def test_empty_result_is_not_an_error(db_client: TestClient):
    result = call(db_client, table="drivers", filters={"city": "火星"})

    assert result["rows"] == []
    assert result["total"] == 0
    assert result["truncated"] is False


def test_blob_is_base64_encoded(db_client: TestClient):
    result = call(db_client, table="drivers", columns=["id", "avatar"], filters={"id": 1})

    assert result["rows"][0]["avatar"] == base64.b64encode(b"\x89PNG").decode("ascii")


def test_null_stays_null(db_client: TestClient):
    result = call(db_client, table="drivers", columns=["id", "rating", "avatar"], filters={"id": 5})

    assert result["rows"][0] == {"id": 5, "rating": None, "avatar": None}


def test_unknown_table_returns_422_with_available_tables(db_client: TestClient):
    response = db_client.post(
        "/v1/tools/execute", json={"tool": "search_database", "arguments": {"table": "ghost"}}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_arguments"
    assert error["details"][0]["type"] == "unknown_table"
    assert "drivers" in error["details"][0]["msg"]


def test_unknown_column_in_projection_returns_422(db_client: TestClient):
    response = db_client.post(
        "/v1/tools/execute",
        json={"tool": "search_database", "arguments": {"table": "drivers", "columns": ["nope"]}},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["details"][0]["type"] == "unknown_column"
    assert error["details"][0]["loc"] == ["columns"]


def test_unknown_column_in_filters_returns_422(db_client: TestClient):
    response = db_client.post(
        "/v1/tools/execute",
        json={"tool": "search_database", "arguments": {"table": "drivers", "filters": {"nope": 1}}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["filters"]


@pytest.mark.parametrize(
    ("field", "value", "type_"),
    [
        ("limit", 0, "greater_than_equal"),
        ("limit", 1001, "less_than_equal"),
        ("offset", -1, "greater_than_equal"),
    ],
)
def test_out_of_range_paging_is_rejected(db_client: TestClient, field, value, type_):
    response = db_client.post(
        "/v1/tools/execute",
        json={"tool": "search_database", "arguments": {"table": "drivers", field: value}},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_arguments"
    assert error["details"][0]["type"] == type_


def test_empty_database_lists_no_tables(sqlite_lib: str):
    client = TestClient(create_app(Settings(plugins=("sqlite",), sqlite_path=sqlite_lib)))

    response = client.post(
        "/v1/tools/execute", json={"tool": "search_database", "arguments": {"table": "drivers"}}
    )

    assert response.status_code == 422
    assert "没有任何表" in response.json()["error"]["details"][0]["msg"]


def test_connection_is_read_only(db_client: TestClient):
    """只读不只是约定：库是以 mode=ro 打开的，写操作会被 SQLite 自己拒绝。"""

    tool = db_client.app.state.registry.get("search_database")

    with pytest.raises(sqlite3.OperationalError):
        tool._connection.execute("CREATE TABLE hacked (id INTEGER)")
