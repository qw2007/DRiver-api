"""阶段 5：request_id 在 HTTP 上的端到端行为。

发号、回显、白名单、以及「错误信封与响应头必须是同一个值」——最后这条是全链路追踪的
前提：Agent 报错时只会把信封贴给人，而人要能拿这个值去日志里 grep。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

GENERATED = re.compile(r"\A[0-9a-f]{32}\Z")
HEADER = "x-request-id"


def test_every_response_carries_an_id(client: TestClient):
    response = client.get("/health")

    assert response.status_code == 200
    assert GENERATED.match(response.headers[HEADER])


def test_caller_supplied_id_is_echoed_verbatim(client: TestClient):
    """上游网关/Agent 自带的 trace id 必须能穿透，否则跨系统追不了。"""
    response = client.get("/health", headers={"X-Request-ID": "trace-abc.1_2"})

    assert response.headers[HEADER] == "trace-abc.1_2"


def test_illegal_id_is_replaced_not_echoed(client: TestClient):
    """不合法的形状不能原样写进响应头与日志（空格、超长都是注入面）。"""
    for supplied in ("bad id", "x" * 65):
        response = client.get("/health", headers={"X-Request-ID": supplied})

        assert response.headers[HEADER] != supplied
        assert GENERATED.match(response.headers[HEADER])


def test_two_requests_get_different_ids(client: TestClient):
    first = client.get("/health").headers[HEADER]
    second = client.get("/health").headers[HEADER]

    assert first != second


def test_error_envelope_reuses_the_response_header(client: TestClient):
    response = client.post("/v1/tools/execute", json={"tool": "ghost", "arguments": {}})

    assert response.status_code == 404
    assert response.json()["error"]["request_id"] == response.headers[HEADER]


def test_request_validation_error_also_carries_the_id(client: TestClient):
    """框架层的 422 与我们的 404 走的是不同 handler，但共用同一个上下文。"""
    response = client.post("/v1/tools/execute", json={"arguments": {}})

    assert response.status_code == 422
    assert response.json()["error"]["request_id"] == response.headers[HEADER]


def test_unmatched_route_still_gets_an_id(client: TestClient):
    """没匹配到任何路由也要有号：这类 404 恰恰是排查「Agent 打错地址」时最需要的。"""
    response = client.get("/v1/nope")

    assert response.status_code == 404
    assert GENERATED.match(response.headers[HEADER])
