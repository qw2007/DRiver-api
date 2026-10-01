"""请求相关性：一个 request_id 贯穿请求、工具调用、日志与错误信封。

零依赖（只用标准库），因为它被最低层复用：``errors.py`` 要在错误信封里带上它，
``tools/executor.py`` 要在工具日志里带上它，``observability/middleware.py`` 要在
响应头里带上它。三处必须是**同一个值**，所以只留一个来源：一个 ``ContextVar``。

**为什么是 ``ContextVar`` 而不是 ``threading.local``**：这里的并发单位是 asyncio 任务，
不是线程——线程本地变量在同一个事件循环里被所有请求共享（request_id 会串号），
而 ``ContextVar`` 随任务复制，天然按请求隔离。

**为什么自带值要过白名单**：这个值会被原样写进响应头与日志行。允许任意字符串，
调用方就能用 ``\n`` 伪造日志行、用超长值把日志撑爆。只接受短、无空白、无换行的形状。
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-ID"
"""响应头名字：与日志字段、错误信封里的 ``request_id`` 一一对应。"""

UNKNOWN_REQUEST_ID = "-"
"""没有请求上下文时给日志用的占位符（例如进程启动期的日志）。"""

#: 外部传入值只接受这个形状：1–64 个 ``[A-Za-z0-9._-]``。
_REQUEST_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

request_id_var: ContextVar[str | None] = ContextVar("driver_request_id", default=None)


def new_request_id() -> str:
    """32 位十六进制（uuid4 去掉连字符）。

    不用递增序号：多副本（阶段 8）时序号会撞车，随机值不需要任何协调就能全局唯一。
    """
    return uuid.uuid4().hex


def normalize_request_id(candidate: str | None) -> str:
    """合法就沿用（调用方自带），否则生成新的。**不写上下文**，纯函数便于单测。"""
    if candidate is not None and _REQUEST_ID_PATTERN.match(candidate):
        return candidate
    return new_request_id()


def ensure_request_id(candidate: str | None = None) -> str:
    """规范化的同时写入当前上下文，返回最终值。

    HTTP 中间件与 MCP 的 ``tools/call`` 都用它：两条协议的 request_id 语义因此一致。
    """
    value = normalize_request_id(candidate)
    request_id_var.set(value)
    return value


def current_request_id() -> str | None:
    """当前上下文的 request_id；不在请求/调用上下文里时是 ``None``。

    ``None`` 与占位符是两件事：``None`` 表示「这里没有请求」（信封里就不该出现
    ``request_id`` 字段），``UNKNOWN_REQUEST_ID`` 只是日志里的显示填充。
    """
    return request_id_var.get()
