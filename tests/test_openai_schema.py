"""OpenAI Schema 适配器契约测试。

跨协议一致性在这里钉死：OpenAI 的 ``parameters`` 必须**原样**等于阶段 1
``GET /v1/tools`` 给出的 ``input_schema``——两个协议看到的是同一份契约，
适配器不许自己改字段、也不许漏工具。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

PATH = "/v1/protocols/openai/tools"


def test_disabled_by_default(client: TestClient) -> None:
    """特性开关默认关：没在 DRIVER_PROTOCOLS 里开的协议就是不存在。"""
    response = client.get(PATH)
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "protocol_not_enabled"
    assert "DRIVER_PROTOCOLS" in error["message"]


def test_tools_mirror_registry_contract(openai_client: TestClient) -> None:
    tools = openai_client.get(PATH).json()["tools"]
    manifests = openai_client.get("/v1/tools").json()

    assert [tool["function"]["name"] for tool in tools] == [
        manifest["name"] for manifest in manifests
    ]
    for tool, manifest in zip(tools, manifests, strict=True):
        assert tool["type"] == "function"
        assert tool["function"]["description"] == manifest["description"]
        assert tool["function"]["parameters"] == manifest["input_schema"]


def test_plugin_tool_is_exported_with_real_schema(openai_db_client: TestClient) -> None:
    tools = openai_db_client.get(PATH).json()["tools"]
    names = [tool["function"]["name"] for tool in tools]
    assert names == ["echo", "search_database"]

    search = next(tool for tool in tools if tool["function"]["name"] == "search_database")
    parameters = search["function"]["parameters"]
    assert parameters["type"] == "object"
    assert "table" in parameters["properties"]
    assert "table" in parameters["required"]


def test_export_shrinks_with_plugin_state(openai_client: TestClient) -> None:
    """没装插件的实例不该导出 search_database（同一份装配逻辑，两个进程各说各的）。"""
    names = [tool["function"]["name"] for tool in openai_client.get(PATH).json()["tools"]]
    assert names == ["echo"]
