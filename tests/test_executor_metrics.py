"""阶段 5：执行管线的指标与日志，以及 ``/metrics`` 端点的边界。

一律用**增量**断言（先抓一次、动作后再抓一次）：指标是进程级累计量，写死绝对值会让用例
依赖执行顺序。这也顺手钉住了「抓取本身也会被记账」这件事。
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.main import create_app
from driver_platform.observability.logging import JsonFormatter
from driver_platform.observability.metrics import CONTENT_TYPE_LATEST, METRICS
from driver_platform.security import parse_api_keys
from driver_platform.tools.base import Tool
from driver_platform.tools.echo import EchoInput, EchoOutput

ECHO_BODY = {"tool": "echo", "arguments": {"message": "hi"}}


def _http_sample(method: str, route: str, status: int) -> str:
    return f'driver_http_requests_total{{method="{method}",route="{route}",status="{status}"}}'


def _tool_sample(tool: str, outcome: str) -> str:
    return f'driver_tool_calls_total{{outcome="{outcome}",tool="{tool}"}}'


def _value(body: str, sample: str) -> float:
    prefix = f"{sample} "
    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix) :])
    return 0.0


def _scrape(client: TestClient) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200, response.text
    return response.text


def _delta(client: TestClient, sample: str, action: Callable[[], Any]) -> float:
    before = _value(_scrape(client), sample)
    action()
    return _value(_scrape(client), sample) - before


@contextmanager
def _json_lines(logger_name: str) -> Iterator[list[dict[str, Any]]]:
    """按**真实 formatter** 收集某支 logger 的输出，而不是另写一套字段断言。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    collected: list[dict[str, Any]] = []
    try:
        yield collected
    finally:
        logger.removeHandler(handler)
        collected.extend(
            json.loads(line) for line in stream.getvalue().splitlines() if line.strip()
        )


@pytest.fixture
def metrics_app() -> FastAPI:
    return create_app(Settings(metrics_enabled=True))


@pytest.fixture
def metrics_client(metrics_app: FastAPI) -> TestClient:
    return TestClient(metrics_app)


@pytest.fixture
def auth_client() -> TestClient:
    """指标开着 **且** 强制凭证：用来验证「抓取端点公开、业务端点照旧」这条边界。"""
    return TestClient(
        create_app(
            Settings(metrics_enabled=True, auth_mode="required", api_keys=parse_api_keys("key-1"))
        )
    )


class BoomTool(Tool[EchoInput, EchoOutput]):
    name = "boom"
    contract_version = "1.0"
    description = "测试用：总是执行失败"
    input_model = EchoInput
    output_model = EchoOutput

    async def execute(self, arguments: EchoInput) -> EchoOutput:
        raise RuntimeError("内部炸了")


# --- 端点边界 -----------------------------------------------------------------


def test_metrics_endpoint_is_absent_until_enabled(client: TestClient):
    """默认关：不是「返回空」，而是**这个路由不存在**。"""
    assert METRICS.enabled is False
    assert client.get("/metrics").status_code == 404


def test_metrics_endpoint_serves_the_prometheus_text_format(metrics_client: TestClient):
    metrics_client.get("/health")  # 先产生一条样本，否则只有 HELP/TYPE 两行

    response = metrics_client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    assert "# TYPE driver_http_requests_total counter" in response.text
    assert _value(response.text, _http_sample("GET", "/health", 200)) >= 1


def test_metrics_endpoint_is_public_even_when_credentials_are_required(auth_client: TestClient):
    """抓取方是集群内的 Prometheus：给它配一把会轮换的钥匙只会让人放弃抓取。"""
    assert auth_client.get("/metrics").status_code == 200
    # 业务端点照旧要钥匙——公开的只有探针与指标两个端点
    assert auth_client.get("/v1/tools").status_code == 401
    assert 'status="401"' in _scrape(auth_client)


# --- HTTP 记账 ----------------------------------------------------------------


def test_the_scrape_itself_is_counted(metrics_client: TestClient):
    """第一次抓取看不到自己（响应体先于记账生成），第二次能看到——顺手证明了记账时机。"""
    metrics_client.get("/metrics")

    assert _value(_scrape(metrics_client), _http_sample("GET", "/metrics", 200)) >= 1


def test_unmatched_route_does_not_become_a_label(metrics_client: TestClient):
    """路由模板才配当标签：原始路径会让时间序列的数量由调用方决定。"""
    delta = _delta(
        metrics_client,
        _http_sample("GET", "unmatched", 404),
        lambda: metrics_client.get("/v1/nope/deep/path"),
    )

    assert delta == 1
    assert 'route="/v1/nope/deep/path"' not in _scrape(metrics_client)


def test_error_status_is_recorded_on_the_route_template(metrics_client: TestClient):
    delta = _delta(
        metrics_client,
        _http_sample("POST", "/v1/tools/execute", 404),
        lambda: metrics_client.post("/v1/tools/execute", json={"tool": "ghost", "arguments": {}}),
    )

    assert delta == 1


# --- 工具记账：outcome 与错误码同一套词汇 ---------------------------------------


def test_successful_call_is_counted_per_tool(metrics_client: TestClient):
    delta = _delta(
        metrics_client,
        _tool_sample("echo", "success"),
        lambda: metrics_client.post("/v1/tools/execute", json=ECHO_BODY),
    )

    assert delta == 1


def test_unknown_tool_collapses_into_a_fixed_label(metrics_client: TestClient):
    """未知工具名是调用方可控的任意输入：指标里统一进 ``unknown``，日志里留原名。"""
    delta = _delta(
        metrics_client,
        _tool_sample("unknown", "tool_not_found"),
        lambda: metrics_client.post("/v1/tools/execute", json={"tool": "ghost", "arguments": {}}),
    )

    assert delta == 1
    assert 'tool="ghost"' not in _scrape(metrics_client)


def test_invalid_arguments_has_its_own_outcome(metrics_client: TestClient):
    delta = _delta(
        metrics_client,
        _tool_sample("echo", "invalid_arguments"),
        lambda: metrics_client.post(
            "/v1/tools/execute", json={"tool": "echo", "arguments": {"message": "hi", "times": 99}}
        ),
    )

    assert delta == 1


def test_unexpected_plugin_failure_is_counted_as_tool_execution_failed(
    metrics_app: FastAPI, metrics_client: TestClient
):
    """outcome 取的是**对外信封里的码**，所以它和 ``code`` 永远对得上。"""
    metrics_app.state.registry.register(BoomTool())

    delta = _delta(
        metrics_client,
        _tool_sample("boom", "tool_execution_failed"),
        lambda: metrics_client.post(
            "/v1/tools/execute", json={"tool": "boom", "arguments": {"message": "hi"}}
        ),
    )

    assert delta == 1


# --- 日志：与指标同源、与响应头同号 --------------------------------------------


def test_tool_call_log_line_matches_the_response_request_id(metrics_client: TestClient):
    with _json_lines("driver_platform.tools") as lines:
        response = metrics_client.post("/v1/tools/execute", json=ECHO_BODY)

    tool_lines = [line for line in lines if line.get("event") == "tool_call"]
    assert len(tool_lines) == 1
    line = tool_lines[0]
    assert line["tool"] == "echo"
    assert line["outcome"] == "success"
    assert line["request_id"] == response.headers["x-request-id"]
    assert line["duration_ms"] >= 0


def test_failed_tool_call_is_logged_with_its_code(metrics_client: TestClient):
    with _json_lines("driver_platform.tools") as lines:
        metrics_client.post("/v1/tools/execute", json={"tool": "ghost", "arguments": {}})

    tool_lines = [line for line in lines if line.get("event") == "tool_call"]
    assert len(tool_lines) == 1
    assert tool_lines[0]["outcome"] == "tool_not_found"
    assert tool_lines[0]["tool"] == "ghost"  # 日志留原始名字：要能看出调用方写了什么


def test_access_log_line_records_the_route_template(metrics_client: TestClient):
    with _json_lines("driver_platform.access") as lines:
        response = metrics_client.get("/health")

    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "http_request"
    assert line["method"] == "GET"
    assert line["path"] == "/health"
    assert line["status"] == 200
    assert line["request_id"] == response.headers["x-request-id"]


def test_unhandled_exception_still_gets_a_line_and_a_sample(
    metrics_app: FastAPI, metrics_client: TestClient
):
    """没被错误处理器接住的异常（框架 500）也必须留下一条记账，否则请求凭空消失。"""

    @metrics_app.get("/boom")
    def boom() -> None:
        raise RuntimeError("炸了")

    client = TestClient(metrics_app, raise_server_exceptions=False)
    with _json_lines("driver_platform.access") as lines:
        response = client.get("/boom")

    assert response.status_code == 500
    assert _value(_scrape(metrics_client), _http_sample("GET", "/boom", 500)) >= 1
    # 这个 500 由最外层 ServerErrorMiddleware 兜底生成，不经过本中间件，
    # 所以响应头里没有 X-Request-ID；但日志行与指标已经带着号记下了。
    assert "x-request-id" not in response.headers
    line = [item for item in lines if item.get("event") == "http_request"][-1]
    assert line["status"] == 500
    assert line["request_id"]
    assert "exception" in line  # traceback 进了日志，而不是被吞掉


def test_non_http_scopes_are_passed_through(metrics_app: FastAPI):
    """lifespan / websocket 原样放行：给它们发号或记账都是错的。"""
    with TestClient(metrics_app):
        pass
