"""阶段 8：契约兼容——把「旧版 Agent 不会因平台升级而失效」钉成测试。

策略写在 ``docs/stages/stage-8.md``（允许的变更 / 破坏性的变更）。这里只测**当前实现确实**满足
那些「允许」项，因为它们是**隐式**行为：请求体与工具入参模型的宽容来自 pydantic 的默认
``extra='ignore'``，没人写下来过——下一个人给某个入参模型加上 ``extra="forbid"``，兼容性就被静默破坏了。

破坏性变更（删字段、改语义、收紧取值、改错误码）不在这里测——它们要么改代码时必然动到这里，
要么需要升 ``contract_version`` 主版本，见同一份文档。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.main import create_app
from driver_platform.plugins.sqlite import SearchDatabaseInput
from driver_platform.security.keys import parse_api_keys
from driver_platform.tools.echo import EchoInput

#: 成功响应的三个键就是**全部**公开契约（老 Agent 只会读这三个）。
SUCCESS_KEYS = {"tool", "contract_version", "result"}
#: 错误信封里老客户端读的是 code / message，details 与 request_id 属可忽略的新增。
REQUIRED_ERROR_KEYS = {"code", "message"}
OPTIONAL_ERROR_KEYS = {"details", "request_id"}
#: 已发布工具的契约版本：改它就是破坏性变更，必须走主版本升级流程。
PUBLISHED_CONTRACT_VERSION = "1.0"


@pytest.fixture
def auth_client() -> TestClient:
    """强制鉴权的客户端：用来钉住错误码与状态码（老客户端按它们分支）。"""
    return TestClient(create_app(Settings(auth_mode="required", api_keys=parse_api_keys("key-1"))))


def _error(response: Any) -> dict[str, Any]:
    payload = response.json()
    assert "error" in payload, payload
    error = payload["error"]
    assert isinstance(error, dict)
    return error


def test_unknown_top_level_fields_are_ignored(client: TestClient) -> None:
    """新版 Agent 多发的顶层字段不能让老平台 422，老 Agent 多发的同样不能被新平台 422。"""
    response = client.post(
        "/v1/tools/execute",
        json={
            "tool": "echo",
            "arguments": {"message": "hi"},
            "agent": "legacy-agent/0.1",  # 老/新客户端多带的元数据
            "trace_id": "abc-123",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"echo": "hi", "times": 1}


def test_unknown_argument_fields_are_ignored(client: TestClient) -> None:
    """工具入参模型默认 ``extra='ignore'``：多给的入参键被忽略，而不是 422。"""
    response = client.post(
        "/v1/tools/execute",
        json={"tool": "echo", "arguments": {"message": "hi", "experimental_flag": True}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"echo": "hi", "times": 1}


def test_arguments_field_can_be_omitted_entirely(client: TestClient) -> None:
    """``arguments`` 是可选的（信封层面）。缺了它是**工具**报无效入参，而不是框架 422：

    框架校验失败回的是 ``{"detail": ...}``，那不是我们的错误信封——老客户端解析不了。
    """
    response = client.post("/v1/tools/execute", json={"tool": "echo"})
    assert response.status_code == 422, response.text
    error = _error(response)
    assert error["code"] == "invalid_arguments"
    assert "detail" not in response.json()


def test_success_response_has_exactly_the_three_contract_keys(client: TestClient) -> None:
    response = client.post(
        "/v1/tools/execute", json={"tool": "echo", "arguments": {"message": "hi"}}
    )
    assert set(response.json()) == SUCCESS_KEYS
    assert response.json()["contract_version"] == PUBLISHED_CONTRACT_VERSION


def test_error_envelope_promises_only_code_and_message(client: TestClient) -> None:
    """错误信封的**承诺**只有 code / message；details 与 request_id 是可有可无的新增。"""
    response = client.post("/v1/tools/execute", json={"tool": "ghost", "arguments": {}})
    assert response.status_code == 404
    error = _error(response)
    assert REQUIRED_ERROR_KEYS <= set(error)
    assert set(error) <= REQUIRED_ERROR_KEYS | OPTIONAL_ERROR_KEYS
    assert error["code"] == "tool_not_found"
    assert error["message"].strip()


def test_error_codes_and_statuses_are_stable(client: TestClient, auth_client: TestClient) -> None:
    """老 Agent 按 ``error.code`` 与状态码分支，所以这两样都不能随手改。"""
    assert (
        client.post("/v1/tools/execute", json={"tool": "ghost"}).status_code,
        _error(client.post("/v1/tools/execute", json={"tool": "ghost"}))["code"],
    ) == (404, "tool_not_found")

    bad_arguments = client.post(
        "/v1/tools/execute", json={"tool": "echo", "arguments": {"message": 42}}
    )
    assert (bad_arguments.status_code, _error(bad_arguments)["code"]) == (422, "invalid_arguments")

    anonymous = auth_client.post(
        "/v1/tools/execute", json={"tool": "echo", "arguments": {"message": "hi"}}
    )
    assert (anonymous.status_code, _error(anonymous)["code"]) == (401, "missing_credentials")

    unknown_key = auth_client.post(
        "/v1/tools/execute",
        json={"tool": "echo", "arguments": {"message": "hi"}},
        headers={"X-API-Key": "nope"},
    )
    assert (unknown_key.status_code, _error(unknown_key)["code"]) == (401, "invalid_credentials")


def test_tool_list_declares_the_published_contract_version(
    client: TestClient, db_client: TestClient
) -> None:
    for http_client in (client, db_client):
        for manifest in http_client.get("/v1/tools").json():
            assert manifest["contract_version"] == PUBLISHED_CONTRACT_VERSION, manifest["name"]


def test_manifest_schema_is_a_superset_of_the_input_model(
    client: TestClient, db_client: TestClient
) -> None:
    """工具清单里的 ``input_schema`` 至少包含入参模型的全部字段与必填项。

    这样「加可选字段」这类向后兼容的演进不会让老 Agent 拿到的 schema 突然少东西；
    反过来（清单里少了字段）说明有人在 manifest 上用 ``exclude`` / 手写 schema 走偏了。
    """
    models = {"echo": EchoInput, "search_database": SearchDatabaseInput}
    for http_client in (client, db_client):
        for manifest in http_client.get("/v1/tools").json():
            schema = manifest["input_schema"]
            model_schema = models[manifest["name"]].model_json_schema()
            assert set(model_schema["properties"]) <= set(schema.get("properties", {}))
            assert set(model_schema.get("required", [])) <= set(schema.get("required", []))
