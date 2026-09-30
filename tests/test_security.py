"""阶段 4：凭证与工具作用域。

这里的每个断言都对应一张表：**谁（哪把钥匙）在什么情况下应该拿到哪个状态码**。
验收原文只有两句话——「无凭证拒绝」「越权被拦」——所以每个用例都在文里写清它钉的是哪一句。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.errors import InvalidConfigurationError
from driver_platform.main import create_app
from driver_platform.security import parse_api_keys

FULL_KEY = "key-full-0001"
ECHO_KEY = "key-echo-0002"
KEYS = f"{FULL_KEY}, {ECHO_KEY}:echo"


def _app(demo_db: str | None = None, *, protocols: tuple[str, ...] = ()) -> FastAPI:
    return create_app(
        Settings(
            plugins=("sqlite",) if demo_db else (),
            sqlite_path=demo_db,
            protocols=protocols,
            auth_mode="required",
            api_keys=parse_api_keys(KEYS),
        )
    )


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# --- 配置：写错就起不来 -------------------------------------------------------


def test_required_mode_without_keys_fails_at_startup() -> None:
    """`required` 却没有钥匙清单 = 一个谁都能进的门，绝不允许静默成立。"""
    with pytest.raises(InvalidConfigurationError) as excinfo:
        Settings.from_env({"DRIVER_AUTH_MODE": "required"})

    assert excinfo.value.code == "invalid_configuration"
    assert "DRIVER_API_KEYS" in excinfo.value.message


def test_unknown_auth_mode_fails_at_startup() -> None:
    with pytest.raises(InvalidConfigurationError) as excinfo:
        Settings(auth_mode="on")

    assert "off" in excinfo.value.message
    assert "required" in excinfo.value.message


def test_keys_parse_into_scopes() -> None:
    keys = parse_api_keys(" a , b:echo|search_database, c:* ")

    assert keys[0].secret == "a"
    assert keys[0].scopes is None  # 不带作用域 = 全部工具
    assert keys[1].scopes == frozenset({"echo", "search_database"})
    assert keys[2].scopes is None  # 显式通配写法
    assert keys[0].fingerprint != keys[1].fingerprint  # 指纹可进日志且不重复


@pytest.mark.parametrize(
    "raw",
    [
        ":echo",  # 缺密钥
        "k:",  # 作用域为空
        "a|b:echo",  # 密钥里混进了作用域分隔符
        "dup, dup",  # 重复密钥（无法判断哪条生效）
        "k:echo:search_database",  # 多个冒号，解析有歧义
    ],
)
def test_bad_key_list_fails_loudly(raw: str) -> None:
    with pytest.raises(InvalidConfigurationError):
        parse_api_keys(raw)


# --- 开放模式只关掉「强制出示」，没关掉「出示了就要对」 -----------------------


def test_open_mode_serves_tools_without_credentials(client: TestClient) -> None:
    assert client.get("/v1/tools").status_code == 200


def test_open_mode_still_rejects_a_wrong_key(client: TestClient) -> None:
    response = client.get("/v1/tools", headers=_bearer("whatever"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


# --- 无凭证拒绝 ---------------------------------------------------------------


def test_missing_credentials_are_rejected() -> None:
    response = TestClient(_app()).post("/v1/tools/execute", json={"tool": "echo", "arguments": {}})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_credentials"


def test_unknown_key_is_rejected() -> None:
    response = TestClient(_app()).get("/v1/tools", headers=_bearer("nope"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_both_header_styles_are_accepted() -> None:
    api = TestClient(_app())

    assert api.get("/v1/tools", headers=_bearer(FULL_KEY)).status_code == 200
    assert api.get("/v1/tools", headers={"X-API-Key": FULL_KEY}).status_code == 200


def test_non_bearer_scheme_is_rejected() -> None:
    response = TestClient(_app()).get("/v1/tools", headers={"Authorization": f"Basic {FULL_KEY}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert "Bearer" in response.json()["error"]["message"]


def test_health_stays_public() -> None:
    """探针不该带凭证：门禁把监控挡在门外，等于拿可用性换假安全。"""
    assert TestClient(_app()).get("/health").json() == {"status": "ok"}


# --- 越权被拦 -----------------------------------------------------------------


def test_tool_listing_follows_the_scope(demo_db: str) -> None:
    api = TestClient(_app(demo_db))

    scoped = api.get("/v1/tools", headers=_bearer(ECHO_KEY)).json()
    full = api.get("/v1/tools", headers=_bearer(FULL_KEY)).json()

    assert [tool["name"] for tool in scoped] == ["echo"]
    assert [tool["name"] for tool in full] == ["echo", "search_database"]


def test_out_of_scope_call_is_403_not_404(demo_db: str) -> None:
    response = TestClient(_app(demo_db)).post(
        "/v1/tools/execute",
        headers=_bearer(ECHO_KEY),
        json={"tool": "search_database", "arguments": {"table": "drivers"}},
    )

    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "permission_denied"
    assert body["details"][0]["type"] == "permission_denied"
    assert "echo" in body["details"][0]["msg"]  # 回的是**该凭证自己**的允许列表


def test_in_scope_call_succeeds(demo_db: str) -> None:
    response = TestClient(_app(demo_db)).post(
        "/v1/tools/execute",
        headers=_bearer(ECHO_KEY),
        json={"tool": "echo", "arguments": {"message": "hi"}},
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"echo": "hi", "times": 1}


def test_unknown_tool_is_still_404_for_a_permitted_key(demo_db: str) -> None:
    """403 说的是「存在但无权」，别把不存在的工具也算成越权。"""
    response = TestClient(_app(demo_db)).post(
        "/v1/tools/execute", headers=_bearer(FULL_KEY), json={"tool": "ghost", "arguments": {}}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "tool_not_found"


def test_plugin_listing_hides_invisible_plugins(demo_db: str) -> None:
    api = TestClient(_app(demo_db))

    assert api.get("/v1/plugins", headers=_bearer(ECHO_KEY)).json() == []
    listed = api.get("/v1/plugins", headers=_bearer(FULL_KEY)).json()
    assert [plugin["plugin_id"] for plugin in listed] == ["sqlite"]
    assert [tool["name"] for tool in listed[0]["tools"]] == ["search_database"]


def test_openai_export_follows_the_scope(demo_db: str) -> None:
    """协议导出必须和作用域一致，否则 Agent 换个协议就绕过了权限。"""
    api = TestClient(_app(demo_db, protocols=("openai",)))
    path = "/v1/protocols/openai/tools"

    scoped = api.get(path, headers=_bearer(ECHO_KEY)).json()["tools"]
    full = api.get(path, headers=_bearer(FULL_KEY)).json()["tools"]

    assert [tool["function"]["name"] for tool in scoped] == ["echo"]
    assert [tool["function"]["name"] for tool in full] == ["echo", "search_database"]
