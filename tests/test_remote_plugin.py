"""远程插件平台侧代理的单元测试：契约校验、错误映射、入参与结果的透传。

网络用 ``httpx.MockTransport`` 打掉：这条线要测的是「远端回什么，平台怎么办」——
畸形清单、非信封错误、乱码响应这些分支用真 HTTP 反而难构造。真 HTTP 路径由
``tests/test_remote_api.py``（走 conftest 里那个线程内的真远端）覆盖。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from driver_platform.errors import (
    InvalidArgumentsError,
    PluginLoadError,
    RemoteToolFailedError,
    RemoteUnavailableError,
)
from driver_platform.plugins.host import PluginHost
from driver_platform.plugins.remote import (
    RemoteClient,
    RemotePlugin,
    RemoteTool,
    normalize_base_url,
)
from driver_platform.tools import ToolRegistry, register_builtin_tools
from driver_platform.tools.executor import execute_tool

BASE_URL = "http://remote.test"

MANIFEST_BODY: dict[str, Any] = {
    "plugin_id": "echo_farm",
    "version": "1.0.0",
    "description": "示例远端",
    "tools": [
        {
            "name": "remote_echo",
            "contract_version": "1.0",
            "description": "回声",
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        }
    ],
}

Handler = Callable[[httpx.Request], httpx.Response]


def _manifest_handler(body: Any = None, *, status: int = 200) -> Handler:
    payload = MANIFEST_BODY if body is None else body

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/manifest"
        return httpx.Response(status, json=payload)

    return handler


def _plugin(handler: Handler, *, plugin_id: str = "echo_farm") -> RemotePlugin:
    return RemotePlugin(plugin_id, BASE_URL, transport=httpx.MockTransport(handler))


def _client(handler: Handler) -> RemoteClient:
    return RemoteClient("echo_farm", BASE_URL, transport=httpx.MockTransport(handler))


def _loaded(plugin: RemotePlugin) -> tuple[ToolRegistry, PluginHost]:
    """把插件装进一个真宿主（含内置工具），拿回注册表与宿主。"""
    registry = ToolRegistry()
    register_builtin_tools(registry)
    host = PluginHost(registry)
    host.load(plugin)
    return registry, host


# --- 地址规整 ---------------------------------------------------------------


def test_normalize_base_url_strips_whitespace_and_trailing_slash():
    assert normalize_base_url("  http://remote.test/  ") == "http://remote.test"
    assert normalize_base_url("https://remote.test/api") == "https://remote.test/api"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "ftp://remote.test", "http://", "http:///path", "remote.test", "//remote.test"],
)
def test_normalize_base_url_rejects_anything_but_http_with_a_host(raw: str):
    with pytest.raises(ValueError) as excinfo:
        normalize_base_url(raw)

    assert "http://" in str(excinfo.value)


# --- 清单 -------------------------------------------------------------------


def test_fetch_manifest_reads_the_contract():
    manifest = _client(_manifest_handler()).fetch_manifest()

    assert manifest.plugin_id == "echo_farm"
    assert manifest.version == "1.0.0"
    assert manifest.description == "示例远端"
    assert [tool.name for tool in manifest.tools] == ["remote_echo"]
    assert manifest.tools[0].input_schema["type"] == "object"


def test_manifest_claiming_another_plugin_id_is_rejected():
    body = {**MANIFEST_BODY, "plugin_id": "someone_else"}

    with pytest.raises(RemoteToolFailedError) as excinfo:
        _client(_manifest_handler(body)).fetch_manifest()

    assert "someone_else" in excinfo.value.message
    assert excinfo.value.code == "remote_tool_failed"


def test_manifest_without_tools_is_rejected():
    with pytest.raises(RemoteToolFailedError) as excinfo:
        _client(_manifest_handler({**MANIFEST_BODY, "tools": []})).fetch_manifest()

    assert "没有任何工具" in excinfo.value.message


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", ""])
def test_manifest_with_bad_version_is_rejected(version: str):
    """版本号是契约的一部分：远端自称 ``1.0`` 就不许装进来（宿主的模式校验管不到装载后）。"""
    plugin = _plugin(_manifest_handler({**MANIFEST_BODY, "version": version}))

    with pytest.raises(RemoteToolFailedError) as excinfo:
        plugin.setup()

    assert "MAJOR.MINOR.PATCH" in excinfo.value.message


def test_malformed_manifest_reports_the_validation_details():
    body = {**MANIFEST_BODY, "tools": [{"contract_version": "1.0"}]}  # 缺 name

    with pytest.raises(RemoteToolFailedError) as excinfo:
        _client(_manifest_handler(body)).fetch_manifest()

    assert "不符合契约" in excinfo.value.message
    assert excinfo.value.details  # 字段级细节照旧透出，方便远端作者自查
    assert excinfo.value.details[0]["type"] == "missing"


def test_unreachable_remote_does_not_leak_the_internal_address():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RemoteUnavailableError) as excinfo:
        _client(handler).fetch_manifest()

    assert excinfo.value.status_code == 502
    assert excinfo.value.code == "remote_unavailable"
    assert BASE_URL not in excinfo.value.message
    assert "ConnectError" in excinfo.value.message


# --- 调用与错误映射 ---------------------------------------------------------


def test_call_tool_returns_the_remote_result():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/execute"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={"tool": "remote_echo", "contract_version": "1.0", "result": {"echo": "hi"}},
        )

    result = asyncio.run(_client(handler).call_tool("remote_echo", {"message": "hi"}))

    assert result == {"echo": "hi"}


def test_call_tool_sends_the_arguments_verbatim():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = httpx.Response(200, content=request.content).json()
        return httpx.Response(200, json={"tool": "remote_echo", "result": {"echo": "hi"}})

    asyncio.run(_client(handler).call_tool("remote_echo", {"message": "hi", "times": 3}))

    assert seen["body"] == {"tool": "remote_echo", "arguments": {"message": "hi", "times": 3}}


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [("invalid_arguments", 422), ("tool_not_found", 404), ("permission_denied", 403)],
)
def test_remote_error_code_and_status_are_preserved(code: str, expected_status: int):
    """远端自己的词汇原样透出：调用方不该为了远端再学一套错误码。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": code, "message": "远端说的原因", "details": [{"a": 1}]}}
        )

    with pytest.raises(RemoteToolFailedError) as excinfo:
        asyncio.run(_client(handler).call_tool("remote_echo", {}))

    assert excinfo.value.code == code
    assert excinfo.value.status_code == expected_status
    assert "远端说的原因" in excinfo.value.message
    assert excinfo.value.details == [{"a": 1}]


def test_unknown_remote_code_stays_502():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": "远端内部炸了", "message": "boom"}})

    with pytest.raises(RemoteToolFailedError) as excinfo:
        asyncio.run(_client(handler).call_tool("remote_echo", {}))

    assert excinfo.value.code == "远端内部炸了"
    assert excinfo.value.status_code == 502


def test_http_error_without_an_envelope_is_rejected():
    """FastAPI 的 ``{"detail": ...}`` 不是我们的信封——远端也得守契约。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "Internal Server Error"})

    with pytest.raises(RemoteToolFailedError) as excinfo:
        asyncio.run(_client(handler).call_tool("remote_echo", {}))

    assert "非契约的错误响应" in excinfo.value.message
    assert excinfo.value.code == "remote_tool_failed"


def test_non_json_response_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(RemoteToolFailedError) as excinfo:
        asyncio.run(_client(handler).call_tool("remote_echo", {}))

    assert "非 JSON 响应" in excinfo.value.message


@pytest.mark.parametrize(
    "payload",
    [
        {"tool": "remote_echo"},
        {"tool": "remote_echo", "result": [1, 2]},
        {"tool": "remote_echo", "result": "hi"},
    ],
)
def test_result_must_be_a_json_object(payload: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(RemoteToolFailedError) as excinfo:
        asyncio.run(_client(handler).call_tool("remote_echo", {}))

    assert "result" in excinfo.value.message


# --- 代理工具与插件 ---------------------------------------------------------


def test_tool_advertises_the_remote_input_schema_verbatim():
    """``input_schema`` 以远端为唯一真相：平台生成的 schema 会让 Agent 看错契约。"""
    plugin = _plugin(_manifest_handler())
    plugin.setup()
    tool = plugin.tools()[0]

    manifest = tool.manifest()

    assert manifest.input_schema == MANIFEST_BODY["tools"][0]["input_schema"]
    assert manifest.name == "remote_echo"
    assert manifest.description == "回声"


def test_plugin_takes_its_identity_from_the_manifest():
    plugin = _plugin(_manifest_handler())

    assert plugin.version == "0.0.0"  # 装载前只是占位
    plugin.setup()

    assert (plugin.version, plugin.description) == ("1.0.0", "示例远端")


def test_tools_returns_a_copy():
    plugin = _plugin(_manifest_handler())
    plugin.setup()

    plugin.tools().clear()

    assert len(plugin.tools()) == 1


def test_execute_tool_passes_arguments_and_result_through():
    """端到端：注册表 → 执行器 → 远端。入参多余字段照发，远端结果不做二次校验。"""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/manifest":
            return httpx.Response(200, json=MANIFEST_BODY)
        seen.append(httpx.Response(200, content=request.content).json())
        return httpx.Response(
            200,
            json={"tool": "remote_echo", "contract_version": "1.0", "result": {"echo": "你好"}},
        )

    registry, _ = _loaded(_plugin(handler))

    result = asyncio.run(
        execute_tool(registry, "remote_echo", {"message": "你好", "extra": {"a": 1}})
    )

    assert result.tool == "remote_echo"
    assert result.contract_version == "1.0"
    assert result.result == {"echo": "你好"}
    assert seen == [{"tool": "remote_echo", "arguments": {"message": "你好", "extra": {"a": 1}}}]


def test_non_object_arguments_are_rejected_before_any_request():
    """平台至少保证「入参是个 JSON 对象」——不然远端收到的是没有意义的请求。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=MANIFEST_BODY)

    registry, _ = _loaded(_plugin(handler))

    with pytest.raises(InvalidArgumentsError):
        asyncio.run(execute_tool(registry, "remote_echo", "not-an-object"))  # type: ignore[arg-type]

    assert paths == ["/manifest"]  # 清单拉过了，但没有任何 execute 请求发出去


def test_tool_name_conflict_rolls_the_whole_plugin_back():
    body = {
        **MANIFEST_BODY,
        "tools": [{**MANIFEST_BODY["tools"][0], "name": "echo"}],  # 撞内置 echo
    }

    with pytest.raises(PluginLoadError) as excinfo:
        _loaded(_plugin(_manifest_handler(body)))

    assert "echo" in excinfo.value.message


def test_unload_makes_the_remote_tool_unavailable():
    registry, host = _loaded(_plugin(_manifest_handler()))

    assert "remote_echo" in registry.names()

    host.unload("echo_farm")

    assert "remote_echo" not in registry.names()
    assert host.plugin_ids() == []


def test_remote_tool_instances_are_bound_to_their_client():
    plugin = _plugin(_manifest_handler())
    plugin.setup()

    assert isinstance(plugin.tools()[0], RemoteTool)


# —— 阶段 8 收口：连接层重试与清单缓存 ——


def test_manifest_is_cached_for_a_minute():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=MANIFEST_BODY)

    client = _client(handler)
    first = client.fetch_manifest()
    second = client.fetch_manifest()

    assert len(calls) == 1, "60 秒内的第二次装载不应该再打远端"
    assert first is second


def test_manifest_fetch_retries_a_connection_failure_once():
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ConnectError("boom", request=request)

    client = _client(handler)
    with pytest.raises(RemoteUnavailableError):
        client.fetch_manifest()

    assert len(attempts) == 2, "连接没建立时可以安全重试一次"


def test_manifest_fetch_recovers_when_the_retry_succeeds():
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=MANIFEST_BODY)

    manifest = _client(handler).fetch_manifest()

    assert manifest.plugin_id == "echo_farm"
    assert len(attempts) == 2


def test_call_tool_does_not_retry_a_timeout():
    # 超时意味着请求**可能已经到了远端**：重试会让一次写操作执行两遍。
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ReadTimeout("slow", request=request)

    client = _client(handler)
    with pytest.raises(RemoteUnavailableError):
        asyncio.run(client.call_tool("remote_echo", {"message": "hi"}))

    assert len(attempts) == 1
