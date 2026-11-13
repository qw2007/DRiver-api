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

TRACEPARENT_HEADER = "traceparent"
"""W3C Trace Context 的传播头：``version-trace_id-parent_id-flags``。"""

#: 只接受 W3C 规定的形状：两个十六进制版本号、32 位 trace_id、16 位 parent_id、2 位 flags。
_TRACEPARENT_PATTERN = re.compile(r"\A([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})\Z")

_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

trace_id_var: ContextVar[str | None] = ContextVar("driver_trace_id", default=None)
span_id_var: ContextVar[str | None] = ContextVar("driver_span_id", default=None)
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


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """解析上游传来的 ``traceparent``，返回 ``(trace_id, parent_span_id)``。

    全零的 trace_id / parent_id 与 ``ff`` 版本按规范视为无效（返回 ``None``，我们自己起一条）。
    纯函数，不写上下文。
    """
    if not value:
        return None
    matched = _TRACEPARENT_PATTERN.match(value.strip().lower())
    if matched is None:
        return None
    version, trace_id, parent_id, _flags = matched.groups()
    if version == "ff" or trace_id == _ZERO_TRACE or parent_id == _ZERO_SPAN:
        return None
    return trace_id, parent_id


def start_trace(parent: str | None = None) -> str:
    """开始一个 span：沿用上游的 trace_id（有的话），自己生成 span_id。

    返回要发给下游的 ``traceparent`` 字符串。HTTP 入口、远程插件调用、AI 与 WebDAV 出网
    都用它——这样一次请求跨进程仍然是一条 trace，而不是每个进程各起一条。
    """
    parsed = parse_traceparent(parent)
    trace_id = parsed[0] if parsed is not None else uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    trace_id_var.set(trace_id)
    span_id_var.set(span_id)
    return f"00-{trace_id}-{span_id}-01"


def current_traceparent() -> str | None:
    """当前 span 的 ``traceparent``；没有 trace 上下文时是 ``None``。"""
    trace_id = trace_id_var.get()
    span_id = span_id_var.get()
    if trace_id is None or span_id is None:
        return None
    return f"00-{trace_id}-{span_id}-01"


def current_trace_id() -> str | None:
    """当前 trace_id（日志里用它把跨进程的链路串起来）。"""
    return trace_id_var.get()


def outbound_trace_headers() -> dict[str, str]:
    """给下游调用的头：没有 trace 上下文就返回空字典，调用方无脑 ``**`` 展开即可。"""
    value = current_traceparent()
    return {} if value is None else {TRACEPARENT_HEADER: value}
