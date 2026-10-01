"""远程插件管理端点的测试：三道门、注册/卸载，以及「下线即不可用」。

这里用的是 conftest 里那个线程内的**真远端**（stdlib ``http.server``），不是 mock 传输层：
注册端点自己构造 ``RemotePlugin``，测试注入不进去——要测就测真 HTTP（顺便证明了
同步端点在线程池里阻塞式拉清单这条路是通的）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.errors import RemoteUnavailableError
from driver_platform.main import create_app
from driver_platform.security.keys import parse_api_keys

PLUGIN_ID = "echo_farm"
TOOL_NAME = "remote_echo"
ADMIN_KEY = "admin-key"
TOOL_KEY = "tool-key"


@pytest.fixture
def remote_client() -> TestClient:
    """开关打开、不强制凭证（默认开放模式）：管理接口可用。"""
    return TestClient(create_app(Settings(remote_plugin_enabled=True)))


@pytest.fixture
def guarded_client() -> TestClient:
    """开关打开 + 强制凭证：``admin-key`` 只有管理作用域，``tool-key`` 只能调 echo。"""
    return TestClient(
        create_app(
            Settings(
                remote_plugin_enabled=True,
                auth_mode="required",
                api_keys=parse_api_keys(f"{ADMIN_KEY}:plugin_admin,{TOOL_KEY}:echo"),
            )
        )
    )


def _register(client: TestClient, base_url: str, *, headers: dict[str, str] | None = None):
    return client.post(
        "/v1/plugins/remote",
        json={"plugin_id": PLUGIN_ID, "base_url": base_url},
        headers=headers,
    )


def _tool_names(client: TestClient, *, headers: dict[str, str] | None = None) -> list[str]:
    return [tool["name"] for tool in client.get("/v1/tools", headers=headers).json()]


# --- 三道门 -----------------------------------------------------------------


def test_management_api_is_disabled_by_default(client: TestClient, remote_url: str):
    response = _register(client, remote_url)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "remote_plugin_disabled"
    assert "DRIVER_REMOTE_PLUGIN_ENABLED" in response.json()["error"]["message"]

    unregister = client.delete(f"/v1/plugins/remote/{PLUGIN_ID}")
    assert unregister.status_code == 404
    assert unregister.json()["error"]["code"] == "remote_plugin_disabled"


def test_disabled_api_still_demands_credentials_when_auth_is_required(remote_url: str):
    """凭证是依赖，先于端点函数执行：所以未带钥匙时拿到的是 401，而不是「接口没开」。"""
    client = TestClient(
        create_app(Settings(auth_mode="required", api_keys=parse_api_keys("k:echo")))
    )

    response = _register(client, remote_url)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_credentials"


def test_flag_on_without_credentials_is_401(remote_url: str):
    client = TestClient(
        create_app(
            Settings(
                remote_plugin_enabled=True, auth_mode="required", api_keys=parse_api_keys("k:echo")
            )
        )
    )

    response = _register(client, remote_url)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_credentials"


def test_key_without_the_admin_scope_is_rejected_but_can_still_use_tools(
    guarded_client, remote_url
):
    headers = {"X-API-Key": TOOL_KEY}

    response = _register(guarded_client, remote_url, headers=headers)

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "permission_denied"
    assert error["details"][0]["msg"] == "需要作用域：plugin_admin"
    # 同一把钥匙照旧能调工具——管理作用域不是「更高级的钥匙」，而是另一个维度
    call = guarded_client.post(
        "/v1/tools/execute", json={"tool": "echo", "arguments": {"message": "hi"}}, headers=headers
    )
    assert call.status_code == 200


def test_admin_key_can_manage_but_cannot_call_tools(guarded_client, remote_url):
    headers = {"X-API-Key": ADMIN_KEY}

    assert _register(guarded_client, remote_url, headers=headers).status_code == 201

    call = guarded_client.post(
        "/v1/tools/execute", json={"tool": "echo", "arguments": {"message": "hi"}}, headers=headers
    )
    assert call.status_code == 403
    assert call.json()["error"]["code"] == "permission_denied"


def test_wildcard_key_can_manage(remote_url: str):
    """全权钥匙（``scopes is None``）也放行——与它在全系统里的身份一致。"""
    client = TestClient(
        create_app(
            Settings(
                remote_plugin_enabled=True,
                auth_mode="required",
                api_keys=parse_api_keys("root-key"),
            )
        )
    )

    assert _register(client, remote_url, headers={"X-API-Key": "root-key"}).status_code == 201


def test_open_mode_anonymous_can_manage(remote_client: TestClient, remote_url: str):
    """开放模式下匿名主体就是全权主体，真正拦住别人的是「开关默认关」。"""
    assert _register(remote_client, remote_url).status_code == 201


# --- 注册与调用 -------------------------------------------------------------


def test_register_publishes_the_remote_tool(remote_client: TestClient, remote_url: str):
    response = _register(remote_client, remote_url)

    assert response.status_code == 201
    body = response.json()
    assert body["plugin_id"] == PLUGIN_ID
    assert body["version"] == "1.0.0"
    assert body["description"] == "测试用远端：把 message 回声回来"
    assert body["tools"] == [{"name": TOOL_NAME, "contract_version": "1.0"}]

    tools = {tool["name"]: tool for tool in remote_client.get("/v1/tools").json()}
    assert tools[TOOL_NAME]["input_schema"]["required"] == ["message"]
    assert [plugin["plugin_id"] for plugin in remote_client.get("/v1/plugins").json()] == [
        PLUGIN_ID
    ]


def test_registered_tool_can_be_called(remote_client: TestClient, remote_url: str):
    _register(remote_client, remote_url)

    response = remote_client.post(
        "/v1/tools/execute",
        json={"tool": TOOL_NAME, "arguments": {"message": "你好"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tool"] == TOOL_NAME
    assert body["contract_version"] == "1.0"
    assert body["result"]["echo"] == "你好"


def test_remote_validation_error_keeps_its_own_code(remote_client: TestClient, remote_url: str):
    """远端的 ``invalid_arguments`` 原样透出：平台不替它编一个自己的错误码。"""
    _register(remote_client, remote_url)

    response = remote_client.post("/v1/tools/execute", json={"tool": TOOL_NAME, "arguments": {}})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_arguments"
    assert "message" in error["message"]


def test_unregister_makes_the_tool_unavailable(remote_client: TestClient, remote_url: str):
    _register(remote_client, remote_url)
    assert TOOL_NAME in _tool_names(remote_client)

    response = remote_client.delete(f"/v1/plugins/remote/{PLUGIN_ID}")

    assert response.status_code == 204
    assert response.content == b""
    assert TOOL_NAME not in _tool_names(remote_client)
    call = remote_client.post("/v1/tools/execute", json={"tool": TOOL_NAME, "arguments": {}})
    assert call.status_code == 404
    assert call.json()["error"]["code"] == "tool_not_found"


def test_unregister_refuses_local_plugins(demo_db: str):
    """本接口只管远程插件：本地插件的工具是启动配置装进来的，那条路不开。"""
    client = TestClient(
        create_app(Settings(remote_plugin_enabled=True, plugins=("sqlite",), sqlite_path=demo_db))
    )

    response = client.delete("/v1/plugins/remote/sqlite")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "plugin_not_found"
    assert "search_database" in _tool_names(client)


def test_unregister_unknown_plugin_is_404(remote_client: TestClient):
    response = remote_client.delete("/v1/plugins/remote/ghost")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "plugin_not_found"


def test_registering_the_same_plugin_twice_is_rejected(remote_client: TestClient, remote_url: str):
    _register(remote_client, remote_url)

    response = _register(remote_client, remote_url)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "plugin_load_failed"


# --- 请求体与远端故障 -------------------------------------------------------


@pytest.mark.parametrize("base_url", ["ftp://remote.test", "remote.test", "http://"])
def test_bad_base_url_is_a_request_error(remote_client: TestClient, base_url: str):
    response = remote_client.post(
        "/v1/plugins/remote", json={"plugin_id": PLUGIN_ID, "base_url": base_url}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_unknown_request_field_is_rejected(remote_client: TestClient, remote_url: str):
    """字段名拼错必须立刻 422，而不是被忽略后变成「插件名叫空」这种怪事。"""
    response = remote_client.post(
        "/v1/plugins/remote", json={"pluginId": PLUGIN_ID, "base_url": remote_url}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_unreachable_remote_is_502_and_keeps_the_request_id(dead_remote_url: str):
    # 超时显式调小：本机连一个没人监听的端口是「超时」而不是立刻被拒（见 conftest 的夹具说明）
    client = TestClient(create_app(Settings(remote_plugin_enabled=True, remote_timeout=0.25)))

    response = _register(client, dead_remote_url)

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "remote_unavailable"
    assert dead_remote_url not in error["message"]  # 不回显内网地址
    assert error["request_id"] == response.headers["x-request-id"]


# --- 启动期装载 -------------------------------------------------------------


def test_remote_plugin_can_be_configured_at_startup(remote_url: str):
    client = TestClient(create_app(Settings(remote_plugins=((PLUGIN_ID, remote_url),))))

    assert TOOL_NAME in _tool_names(client)
    call = client.post(
        "/v1/tools/execute", json={"tool": TOOL_NAME, "arguments": {"message": "hi"}}
    )
    assert call.status_code == 200
    assert call.json()["result"]["echo"] == "hi"


def test_startup_fails_when_a_configured_remote_is_down(dead_remote_url: str):
    """「启动即失败」是刻意的：宁可起不来，也不要带着一半坏工具对外服务。"""
    with pytest.raises(RemoteUnavailableError):
        create_app(Settings(remote_plugins=((PLUGIN_ID, dead_remote_url),), remote_timeout=0.25))
