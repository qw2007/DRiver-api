"""W3C ``traceparent`` 的进出：进来接着上游，出去带给下游。

这一层只做「一条 trace 跨进程还是同一条」，不做 span 树、不上 OTel SDK——理由写在
``observability/context.py`` 的模块注释里。
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.main import create_app
from driver_platform.observability.context import (
    TRACEPARENT_HEADER,
    current_trace_id,
    current_traceparent,
    outbound_trace_headers,
    parse_traceparent,
    start_trace,
)

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"
VALID = f"00-{TRACE}-{PARENT}-01"

_SPAN = re.compile(r"\A00-([0-9a-f]{32})-([0-9a-f]{16})-01\Z")


def test_parse_accepts_a_well_formed_header() -> None:
    assert parse_traceparent(VALID) == (TRACE, PARENT)
    # 大写与两侧空白都按规范容忍
    assert parse_traceparent(f"  {VALID.upper()}  ") == (TRACE, PARENT)


def test_parse_rejects_what_the_spec_calls_invalid() -> None:
    for bad in (
        None,
        "",
        "00-abc-def-01",
        f"ff-{TRACE}-{PARENT}-01",
        f"00-{'0' * 32}-{PARENT}-01",
        f"00-{TRACE}-{'0' * 16}-01",
        f"01-{TRACE}-{PARENT}-01-extra",
        "not-a-traceparent",
    ):
        assert parse_traceparent(bad) is None, bad


def test_start_trace_inherits_the_upstream_trace_id_but_mints_a_new_span() -> None:
    header = start_trace(VALID)
    assert header == current_traceparent()
    assert _SPAN.match(header) is not None
    assert current_trace_id() == TRACE
    span = header.split("-")[2]
    assert span != PARENT  # 本进程是新的一跳


def test_start_trace_without_upstream_makes_a_new_trace() -> None:
    header = start_trace(None)
    assert _SPAN.match(header) is not None
    assert current_trace_id() == header.split("-")[1]
    assert current_trace_id() is not None and current_trace_id() != TRACE


def test_outbound_headers_follow_the_current_span() -> None:
    assert outbound_trace_headers() == {}  # 没有 trace 上下文就不乱发头
    start_trace(VALID)
    assert outbound_trace_headers() == {TRACEPARENT_HEADER: current_traceparent()}


def test_http_request_continues_the_upstream_trace() -> None:
    client = TestClient(create_app(Settings()))
    response = client.get("/health", headers={TRACEPARENT_HEADER: VALID})
    assert response.status_code == 200
    sent = response.headers[TRACEPARENT_HEADER]
    matched = _SPAN.match(sent)
    assert matched is not None
    assert matched.group(1) == TRACE  # 同一个 trace
    assert matched.group(2) != PARENT  # 但换了本进程的 span


def test_http_request_without_a_header_still_gets_one() -> None:
    client = TestClient(create_app(Settings()))
    first = client.get("/health").headers[TRACEPARENT_HEADER]
    second = client.get("/health").headers[TRACEPARENT_HEADER]
    assert _SPAN.match(first) is not None
    # 每次请求各自起一条 trace（没有上游可接时）
    assert first.split("-")[1] != second.split("-")[1]


def test_http_request_ignores_a_broken_header() -> None:
    client = TestClient(create_app(Settings()))
    sent = client.get("/health", headers={TRACEPARENT_HEADER: "garbage"}).headers[
        TRACEPARENT_HEADER
    ]
    assert _SPAN.match(sent) is not None
    assert sent.split("-")[1] != TRACE


def test_remote_plugin_calls_carry_the_current_trace() -> None:
    """出网点只认 ``outbound_trace_headers()``：这里用它的同步形状验证一次真实的头。"""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(
            200, json={"tool": "remote_echo", "contract_version": "1.0", "result": {}}
        )

    start_trace(VALID)
    client = httpx.Client(transport=httpx.MockTransport(handler), headers=outbound_trace_headers())
    client.get("http://remote.test/manifest")
    assert seen[0]["traceparent"] == current_traceparent()
    assert seen[0]["traceparent"].split("-")[1] == TRACE
