"""日志：一行一个 JSON 对象（或 ``key=value``），并自动带上 request_id。

**为什么只用标准库**：我们的字段契约只有一层（``ts``/``level``/``logger``/``msg``/
``request_id`` + 埋点键）。引 structlog 等于为 30 行格式化代码增加一个依赖，还要多学
一套它自己的配置语义——而日志是排障时才看的东西，越可预测越好。

两条刻意的边界：

1. handler 只挂在 ``driver_platform`` 这一支 logger 上，``propagate`` 保持为真。
   不碰 root，就不会和 uvicorn、pytest（``caplog``）的 handler 互相挤掉。
2. 输出到 **stderr**。stdio MCP 的 stdout 是协议通道，日志混进去就是协议损坏。

字段规则：``LogRecord`` 的标准属性之外的一切都算**埋点字段**（``event``、``tool``、
``duration_ms``…），原样带出。所以加一个埋点不需要改 formatter——formatter 不为字段名
写死清单，只为「标准属性」写死黑名单。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from driver_platform.observability.context import current_request_id

LOG_FORMAT_TEXT = "text"
LOG_FORMAT_JSON = "json"
LOG_FORMATS = (LOG_FORMAT_TEXT, LOG_FORMAT_JSON)

DEFAULT_LOG_LEVEL = "INFO"
LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

PACKAGE_LOGGER = "driver_platform"
"""我们唯一配置的那支 logger：自己的日志自己格式化，别人的日志不动。"""

_HANDLER_FLAG = "_driver_platform_handler"
"""给 handler 打标记：``configure_logging`` 幂等靠它认出「上次装的是我们」。"""

_RESERVED: frozenset[str] = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "exc_text",
}
"""``LogRecord`` 自带属性：这些不进「埋点字段」。"""


def timestamp(created: float) -> str:
    """ISO 8601 UTC，毫秒精度、``Z`` 结尾：能被任何日志系统直接解析。"""
    return (
        datetime.fromtimestamp(created, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """埋点字段＝标准属性之外的键（``request_id`` 单独处理，不重复带出）。"""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED and key != "request_id"
    }


def _request_id_of(record: logging.LogRecord) -> str | None:
    """显式 ``extra={"request_id": ...}`` 优先；否则取当前上下文的。

    优先显式值，是为了让「事后补记的日志」（例如请求已结束、在另一个任务里写日志）
    也能带上正确的 id。
    """
    explicit = getattr(record, "request_id", None)
    return explicit if isinstance(explicit, str) and explicit else current_request_id()


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象。``ensure_ascii=False``：中文原样输出，不转 ``\\uXXXX``。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = _request_id_of(record)
        if request_id is not None:
            payload["request_id"] = request_id
        payload.update(extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class KeyValueFormatter(logging.Formatter):
    """``ts level logger msg key=value …``：本地 ``grep`` 友好，且仍然带 request_id。"""

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            timestamp(record.created),
            record.levelname,
            record.name,
            record.getMessage(),
        ]
        request_id = _request_id_of(record)
        if request_id is not None:
            parts.append(f"request_id={request_id}")
        parts.extend(f"{key}={value}" for key, value in extra_fields(record).items())
        line = " ".join(parts)
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str = DEFAULT_LOG_LEVEL, fmt: str = LOG_FORMAT_TEXT) -> None:
    """给 ``driver_platform`` 这支 logger 装上（或换掉）我们的 handler。

    幂等：先摘掉自己上次装的（带标记的）handler，再装新的——反复调用不会让日志打印两遍。

    ``level`` / ``fmt`` 的合法性由 ``config.Settings`` 在启动时把住（写错就起不来）；
    对直接调用者这里保持宽容：级别大小写不敏感、不认识就用默认，格式不认识就退回 text。
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    for existing in list(logger.handlers):
        if getattr(existing, _HANDLER_FLAG, False):
            logger.removeHandler(existing)

    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_FLAG, True)
    handler.setFormatter(JsonFormatter() if fmt == LOG_FORMAT_JSON else KeyValueFormatter())
    logger.addHandler(handler)
    resolved = level.strip().upper()
    logger.setLevel(resolved if resolved in LEVEL_NAMES else DEFAULT_LOG_LEVEL)
