"""工具接口的契约测试：成功形状、未知工具、非法参数三类必须稳定。"""

from driver_platform.tools.base import Tool
from driver_platform.tools.echo import EchoInput, EchoOutput


def test_list_tools_returns_manifests(client):
    response = client.get("/v1/tools")

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body] == ["echo"]
    assert body[0]["contract_version"] == "1.0"
    assert body[0]["input_schema"]["properties"]["message"]["type"] == "string"


def test_execute_echo_uses_default_arguments(client):
    response = client.post(
        "/v1/tools/execute", json={"tool": "echo", "arguments": {"message": "hi"}}
    )

    assert response.status_code == 200
    assert response.json() == {
        "tool": "echo",
        "contract_version": "1.0",
        "result": {"echo": "hi", "times": 1},
    }


def test_execute_echo_repeats_message(client):
    response = client.post(
        "/v1/tools/execute",
        json={"tool": "echo", "arguments": {"message": "hi", "times": 3}},
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"echo": "hi\nhi\nhi", "times": 3}


def test_unknown_tool_returns_404_with_error_envelope(client):
    response = client.post("/v1/tools/execute", json={"tool": "ghost", "arguments": {}})

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "tool_not_found", "message": "未注册的工具：ghost", "details": None}
    }


def test_invalid_arguments_returns_422(client):
    response = client.post(
        "/v1/tools/execute",
        json={"tool": "echo", "arguments": {"message": "hi", "times": 99}},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_arguments"
    assert error["details"][0]["loc"] == ["times"]
    assert error["details"][0]["type"] == "less_than_equal"


def test_missing_required_argument_returns_422(client):
    response = client.post("/v1/tools/execute", json={"tool": "echo", "arguments": {}})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_arguments"


def test_malformed_request_body_returns_422(client):
    response = client.post("/v1/tools/execute", json={"arguments": {"message": "hi"}})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"][0]["loc"] == ["body", "tool"]


def test_arguments_must_be_an_object(client):
    response = client.post("/v1/tools/execute", json={"tool": "echo", "arguments": "hi"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


class BoomTool(Tool[EchoInput, EchoOutput]):
    """执行即抛错的工具，用来验证异常被包成稳定 code。"""

    name = "boom"
    contract_version = "1.0"
    description = "测试用：总是执行失败"
    input_model = EchoInput
    output_model = EchoOutput

    async def execute(self, arguments: EchoInput) -> EchoOutput:
        raise RuntimeError("内部炸了")


def test_registry_can_be_extended_through_public_api(client, registry):
    """注册表可扩展 —— 阶段 2 的插件宿主走同一个口子。"""

    registry.register(BoomTool())

    response = client.post(
        "/v1/tools/execute", json={"tool": "boom", "arguments": {"message": "hi"}}
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "tool_execution_failed",
            "message": "工具 boom 执行失败",
            "details": None,
        }
    }
