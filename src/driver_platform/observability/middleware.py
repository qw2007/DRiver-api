"""纯 ASGI 中间件：request_id、访问日志、HTTP 指标。

**为什么不用 Starlette 的 ``BaseHTTPMiddleware``**：它给每个请求再包一层 anyio task，
``contextvars`` 的传播语义随之改变（request_id 会丢或串到别的请求上），而且它拿到的是
包装后的 response，看不到 ``http.response.start`` 的原始消息。这里自己实现 ASGI 接口，
换来两件必须做到的事：**(a)** 在响应头真正发出前把 ``X-Request-ID`` 塞进去（含 404/500）；
**(b)** 在那一刻读到真实 status。

**为什么标签用路由模板**：``/v1/tools/{name}`` 的原始路径会让每个工具名变成一条时间序列
（基数随调用方输入膨胀）——Prometheus 最经典的翻车方式。未匹配到路由的请求统一归
``unmatched``，原始路径只留在日志里。
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from driver_platform.observability.context import REQUEST_ID_HEADER, ensure_request_id
from driver_platform.observability.metrics import METRICS, Metrics

UNMATCHED_ROUTE = "unmatched"
"""没有匹配到任何路由时的 ``route`` 标签（例如 404 的乱路径）。"""

UNKNOWN_METHOD = "UNKNOWN"

_logger = logging.getLogger("driver_platform.access")

_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("ascii")


class ObservabilityMiddleware:
    """一次 HTTP 请求的可观测性收在这一个地方：进来发号，出去记账。"""

    def __init__(self, app: Any, metrics: Metrics = METRICS) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # websocket / lifespan 原样放行：本阶段只对 HTTP 请求发号。
            await self.app(scope, receive, send)
            return

        method = scope.get("method") or UNKNOWN_METHOD
        request_id = ensure_request_id(_header(scope, _HEADER_BYTES))
        started = perf_counter()
        status = 500  # 兜底：即使响应头都没发出来（连接中断），也要有一条记账

        async def send_with_request_id(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                raw = message.setdefault("headers", [])
                # 先去重再追加：同一个头出现两次，客户端看到哪个是实现定义的。
                headers = [(k, v) for k, v in raw if k.lower() != _HEADER_BYTES]
                headers.append((_HEADER_BYTES, request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            # 交回上层（ServerErrorMiddleware）之前先把 500 与栈记下来，
            # 否则这条请求在指标里消失、在日志里只剩别人的一行 traceback。
            _logger.exception(
                "http_request",
                extra=self._observe(method, _route_of(scope), status, perf_counter() - started),
            )
            raise
        else:
            _logger.info(
                "http_request",
                extra=self._observe(method, _route_of(scope), status, perf_counter() - started),
            )

    def _observe(self, method: str, route: str, status: int, duration: float) -> dict[str, Any]:
        """记一次 HTTP 请求，并返回与之同源的日志字段。

        指标与日志一起产出（而不是各写一份），是为了让它们描述**同一个事实**：
        谁也不会出现「指标说 200、日志说 500」的分叉。
        """
        self.metrics.observe_http(method, route, status, duration)
        return {
            "event": "http_request",
            "method": method,
            "path": route,
            "status": status,
            "duration_ms": round(duration * 1000, 3),
        }


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    """按字节比头名（HTTP 头大小写不敏感），返回可解码的字符串值。

    解码用 ``latin-1``：任意字节都不会在这里抛错，非法字符交给 request_id 的白名单拒绝。
    """
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _route_of(scope: dict[str, Any]) -> str:
    """路由**模板**（``/v1/tools/{name}``）；没匹配上就是 ``unmatched``。"""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED_ROUTE
