"""指标：HTTP 三条线与工具调用，用 Prometheus 文本格式暴露。

**为什么埋点收在 ``track_tool_call()`` 里**：执行管线的失败路径有四种（越权 / 非法入参 /
未知工具 / 工具抛错），写成 ``try/finally`` 会在 ``execute_tool`` 里长出四份重复代码，
而且迟早有人补第五种时忘了记账。上下文管理器在异常**抛出时**读 ``PlatformError.code``，
把「错误码」与「outcome 标签」钉成同一套词汇——能按 ``error.code`` 分支的地方，
就能按 ``outcome`` 建面板，不需要二次翻译表。

**为什么指标与 ``event=tool_call`` 日志出自同一个地方**：两者都由 outcome 决定，
分开写就会出现「指标说 success、日志说 failed」这种谁也说不清的分叉。同一条记录、
两种出口，只有一处计算 outcome 才能保证它们永远一致。

**为什么默认关**：``enabled=False`` 时连计时都不做（不是记完再丢），本地开发与单测
不必为了「不产生指标」去改调用点。开关打开才意味着有人要抓——默认关就是默认不暴露。

**为什么每个 ``Metrics`` 用私有 ``CollectorRegistry``**：默认 registry 是进程级全局，
测试里第二次 ``create_app()`` 会在注册同名指标时炸 ``Duplicated timeseries``。
私有 registry 让「这个应用自己的指标」成为事实，``render()`` 也只输出自己那一份。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from driver_platform.errors import PlatformError

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
"""Prometheus 文本格式的内容类型（与 client 库的常量保持同一字面值，但不受其版本影响）。"""

OUTCOME_SUCCESS = "success"
"""成功调用的 outcome 标签。"""

UNKNOWN_TOOL_LABEL = "unknown"
"""未注册工具名统一进这个标签。"""

INTERNAL_ERROR_CODE = "internal_error"
"""非预期异常的 outcome 标签：即使还没走到信封，也用信封里的词汇。"""

#: 默认桶（秒）：5ms–10s。覆盖「本地 SQLite 查询」到「明显卡住」的整个区间。
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_logger = logging.getLogger("driver_platform.tools")


def _millis(seconds: float) -> float:
    """日志里用毫秒：看延迟时没人愿意数小数点的位置。"""
    return round(seconds * 1000, 3)


class Metrics:
    """一组进程内指标。一个应用一个实例（默认实例见模块级 ``METRICS``）。"""

    def __init__(self) -> None:
        self.enabled = False
        registry = CollectorRegistry()
        self.http_requests = Counter(
            "driver_http_requests_total",
            "HTTP 请求总数（QPS 与错误率的来源）",
            ("method", "route", "status"),
            registry=registry,
        )
        self.http_duration = Histogram(
            "driver_http_request_duration_seconds",
            "HTTP 端到端延迟",
            ("method", "route"),
            buckets=DURATION_BUCKETS,
            registry=registry,
        )
        self.tool_calls = Counter(
            "driver_tool_calls_total",
            "工具调用总数（HTTP 与 MCP 共用执行管线）",
            ("tool", "outcome"),
            registry=registry,
        )
        self.tool_duration = Histogram(
            "driver_tool_duration_seconds",
            "工具执行管线耗时",
            ("tool", "outcome"),
            buckets=DURATION_BUCKETS,
            registry=registry,
        )
        self._registry = registry

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    def observe_http(self, method: str, route: str, status: int, duration: float) -> None:
        """记一次 HTTP 请求。``route`` 必须是路由**模板**，不能是原始路径（基数会炸）。"""
        if not self.enabled:
            return
        self.http_requests.labels(method=method, route=route, status=str(status)).inc()
        self.http_duration.labels(method=method, route=route).observe(duration)

    def observe_tool(self, tool: str, outcome: str, duration: float) -> None:
        """记一次工具调用。``tool`` 取注册表里的名字，未知工具用固定标签。"""
        if not self.enabled:
            return
        self.tool_calls.labels(tool=tool, outcome=outcome).inc()
        self.tool_duration.labels(tool=tool, outcome=outcome).observe(duration)

    @contextmanager
    def track_tool_call(self, tool: str, *, metric_label: str | None = None) -> Iterator[None]:
        """把一段工具执行包成「指标 + 一条 ``event=tool_call`` 日志」。

        ``tool`` 是**请求里的名字**，进日志（排障时要知道调用方到底写了什么）；
        ``metric_label`` 是**指标标签**，默认等于 ``tool``。两者分开是因为一件硬约束：
        未知工具名是调用方可控的任意输入，直接当标签就是把时间序列的基数交给外部决定
        （Prometheus 最经典的翻车方式），所以调用方对未知工具传固定标签，而原始名字
        仍然留在日志里。

        outcome 由 ``PlatformError.code`` 决定，与对外错误信封同一套词汇。
        """
        label = tool if metric_label is None else metric_label
        if not self.enabled:
            yield
            return

        started = perf_counter()
        try:
            yield
        except PlatformError as exc:
            duration = perf_counter() - started
            self.observe_tool(label, exc.code, duration)
            _logger.info(
                "tool_call",
                extra={
                    "event": "tool_call",
                    "tool": tool,
                    "outcome": exc.code,
                    "duration_ms": _millis(duration),
                },
            )
            raise
        except Exception:
            # 非预期异常：执行管线会把对外错误变成 tool_execution_failed，
            # 这里先用同一套词汇记账，避免出现信封里没有的 outcome。
            duration = perf_counter() - started
            self.observe_tool(label, INTERNAL_ERROR_CODE, duration)
            _logger.exception(
                "tool_call",
                extra={
                    "event": "tool_call",
                    "tool": tool,
                    "outcome": INTERNAL_ERROR_CODE,
                    "duration_ms": _millis(duration),
                },
            )
            raise
        else:
            duration = perf_counter() - started
            self.observe_tool(label, OUTCOME_SUCCESS, duration)
            _logger.info(
                "tool_call",
                extra={
                    "event": "tool_call",
                    "tool": tool,
                    "outcome": OUTCOME_SUCCESS,
                    "duration_ms": _millis(duration),
                },
            )

    def render(self) -> bytes:
        """Prometheus 文本格式的当前快照。"""
        return generate_latest(self._registry)


METRICS = Metrics()
"""默认指标集：``create_app()`` 按配置 enable/disable，执行管线直接引用它。

进程级单例是刻意的：Prometheus 抓的就是**这个进程**的数字，多副本时各副本各暴露一份，
由 Prometheus 侧聚合（阶段 8），不需要在应用内做跨进程合并。
"""
