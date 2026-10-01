"""阶段 5：request_id 的发号与白名单。

``context.py`` 是**三处共用的唯一来源**（响应头、日志行、错误信封），所以先把纯函数钉死：
调用方自带的值要不被改写、不合法的形状要被换掉、发出来的号要能被读到。
"""

from __future__ import annotations

import re

from driver_platform.observability.context import (
    REQUEST_ID_HEADER,
    current_request_id,
    ensure_request_id,
    new_request_id,
    normalize_request_id,
)

GENERATED = re.compile(r"\A[0-9a-f]{32}\Z")
"""服务端自己发的号：32 位十六进制。"""


def test_header_name_is_the_documented_one():
    assert REQUEST_ID_HEADER == "X-Request-ID"


def test_generated_id_is_32_hex_and_repeatable_free():
    first = new_request_id()

    assert GENERATED.match(first)
    assert first != new_request_id()  # 随机值：不需要任何协调也不会撞


def test_well_formed_candidate_is_kept_verbatim():
    """调用方自带的值要能穿透（这正是「按一个 id 追全链路」的前提）。"""
    for candidate in ("abc", "A-1_b.2", "x" * 64):
        assert normalize_request_id(candidate) == candidate


def test_malformed_candidate_is_replaced():
    """换行能伪造日志行、超长能撑爆日志、空白会破坏字段解析——一律换掉。"""
    for candidate in (None, "", "   ", "has space", "line\nbreak", "x" * 65, "中文"):
        assert GENERATED.match(normalize_request_id(candidate))


def test_normalize_does_not_touch_the_context():
    """纯函数：规范化不等于「这个请求有号」——否则信封里会冒出不该有的字段。"""
    normalize_request_id("abc")

    assert current_request_id() is None


def test_ensure_sets_and_returns_the_same_value():
    assert ensure_request_id("abc") == "abc"
    assert current_request_id() == "abc"


def test_ensure_generates_when_nothing_was_supplied():
    value = ensure_request_id()

    assert GENERATED.match(value)
    assert current_request_id() == value


def test_ensure_overwrites_the_previous_value():
    """同一个上下文里连续两次调用（MCP 每次 ``tools/call`` 都发号）不能沿用旧号。"""
    ensure_request_id("first")
    ensure_request_id()

    assert current_request_id() != "first"
