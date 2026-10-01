"""阶段 5：日志格式与幂等安装。

日志是排障的最后一道防线，所以这里钉四件事：字段能被机器解析、埋点字段不会被吞、
request_id 会自动跟上、``configure_logging`` 重复调用不会长出第二个 handler
（否则每行日志打印两遍，而人往往以为是自己看花了眼）。
"""

from __future__ import annotations

import json
import logging
import sys

import pytest

from driver_platform.observability.context import ensure_request_id
from driver_platform.observability.logging import (
    DEFAULT_LOG_LEVEL,
    LOG_FORMAT_JSON,
    PACKAGE_LOGGER,
    JsonFormatter,
    KeyValueFormatter,
    configure_logging,
)

HANDLER_FLAG = "_driver_platform_handler"


def _record(msg: str = "tool_call", **extra: object) -> logging.LogRecord:
    """造一条 ``LogRecord``：埋点字段就是 ``extra``，与真实调用点写法一致。"""
    record = logging.LogRecord(
        name="driver_platform.tools",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


@pytest.fixture(autouse=True)
def restore_package_logger():
    """用完把 logger 恢复原状：``configure_logging`` 改的是全局对象。"""
    logger = logging.getLogger(PACKAGE_LOGGER)
    handlers = list(logger.handlers)
    level = logger.level
    yield
    logger.handlers = handlers
    logger.setLevel(level)


# --- 格式：字段契约 -----------------------------------------------------------


def test_json_line_carries_the_documented_fields():
    ensure_request_id("req-1")

    payload = json.loads(
        JsonFormatter().format(
            _record(event="tool_call", tool="echo", outcome="success", duration_ms=3.5)
        )
    )

    assert payload["ts"].endswith("Z")  # ISO 8601 UTC，任何采集系统都能直接解析
    assert payload["level"] == "INFO"
    assert payload["logger"] == "driver_platform.tools"
    assert payload["msg"] == "tool_call"
    assert payload["request_id"] == "req-1"
    assert payload["event"] == "tool_call"
    assert payload["tool"] == "echo"
    assert payload["outcome"] == "success"
    assert payload["duration_ms"] == 3.5


def test_json_keeps_chinese_readable():
    line = JsonFormatter().format(_record(msg="工具调用成功"))

    assert "工具调用成功" in line
    assert "\\u" not in line  # ensure_ascii=False：不给中文转义成 \uXXXX


def test_explicit_request_id_wins_over_the_context():
    """请求结束之后补记的日志也要能带上正确的号。"""
    ensure_request_id("from-context")

    payload = json.loads(JsonFormatter().format(_record(request_id="from-extra")))

    assert payload["request_id"] == "from-extra"


def test_a_record_without_any_request_context_has_no_field():
    """没有请求就没有字段——而不是填一个 ``-`` 假装有（``-`` 只用于显示）。"""
    payload = json.loads(JsonFormatter().format(_record()))

    assert "request_id" not in payload


def test_traceback_is_serialized_in_its_own_field():
    try:
        raise ValueError("炸了")
    except ValueError:
        record = _record()
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: 炸了" in payload["exception"]


def test_key_value_line_is_greppable():
    ensure_request_id("req-2")

    line = KeyValueFormatter().format(_record(event="http_request", status=200))

    assert "INFO" in line
    assert "request_id=req-2" in line
    assert "event=http_request" in line
    assert "status=200" in line
    assert line.startswith("20")  # ISO 时间戳开头


# --- 安装：幂等与边界 ---------------------------------------------------------


def test_handler_is_installed_on_the_package_logger_only():
    configure_logging()

    ours = [
        h for h in logging.getLogger(PACKAGE_LOGGER).handlers if getattr(h, HANDLER_FLAG, False)
    ]
    assert len(ours) == 1
    # 不碰 root：否则会和 uvicorn、pytest 的 handler 互相挤掉
    assert not [h for h in logging.getLogger().handlers if getattr(h, HANDLER_FLAG, False)]
    assert logging.getLogger(PACKAGE_LOGGER).propagate is True  # caplog 仍然收得到


def test_configuring_twice_keeps_exactly_one_handler():
    configure_logging()
    configure_logging()

    ours = [
        h for h in logging.getLogger(PACKAGE_LOGGER).handlers if getattr(h, HANDLER_FLAG, False)
    ]
    assert len(ours) == 1


def test_logs_go_to_stderr():
    """stdio MCP 的 stdout 是协议通道：日志混进去就是协议损坏。"""
    configure_logging()

    handler = next(
        h for h in logging.getLogger(PACKAGE_LOGGER).handlers if getattr(h, HANDLER_FLAG, False)
    )
    assert handler.stream is sys.stderr


def test_json_format_is_selected_by_name():
    configure_logging(fmt=LOG_FORMAT_JSON)

    handler = next(
        h for h in logging.getLogger(PACKAGE_LOGGER).handlers if getattr(h, HANDLER_FLAG, False)
    )
    assert isinstance(handler.formatter, JsonFormatter)


def test_level_is_case_insensitive():
    configure_logging("debug")

    assert logging.getLogger(PACKAGE_LOGGER).level == logging.DEBUG


def test_unknown_level_falls_back_to_the_default():
    """启动期的合法性由 Settings 把住；直接调用这一层只要求它别炸。"""
    configure_logging("SHOUT")

    assert logging.getLogger(PACKAGE_LOGGER).level == logging.getLevelName(DEFAULT_LOG_LEVEL)


def test_unknown_format_falls_back_to_text():
    configure_logging(fmt="logfmt")

    handler = next(
        h for h in logging.getLogger(PACKAGE_LOGGER).handlers if getattr(h, HANDLER_FLAG, False)
    )
    assert isinstance(handler.formatter, KeyValueFormatter)
