"""阶段 5：指标本身的形状——标签、词表、开关、多实例不冲突。

这一层用**私有的** ``Metrics()`` 而不是模块级 ``METRICS``：这里的断言要精确到数字，
而单例会被同进程的其他用例累加（指标本来就是进程级的累计量，不该为测试改设计）。
"""

from __future__ import annotations

import pytest

from driver_platform.errors import ToolNotFoundError
from driver_platform.observability.metrics import (
    DURATION_BUCKETS,
    INTERNAL_ERROR_CODE,
    OUTCOME_SUCCESS,
    UNKNOWN_TOOL_LABEL,
    Metrics,
)


def _body(metrics: Metrics) -> str:
    return metrics.render().decode("utf-8")


def _value(body: str, sample: str) -> float:
    """取一条时间序列的当前值；没有这条序列就是 0（比 ``in`` 更能表达「一条都没记」）。"""
    prefix = f"{sample} "
    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix) :])
    return 0.0


def test_metrics_start_disabled():
    """默认关是契约的一部分：关着的时候连计时都不做。"""
    assert Metrics().enabled is False


def test_disabled_metrics_record_nothing():
    metrics = Metrics()

    metrics.observe_http("GET", "/health", 200, 0.001)
    metrics.observe_tool("echo", OUTCOME_SUCCESS, 0.001)

    body = _body(metrics)
    assert (
        _value(body, 'driver_http_requests_total{method="GET",route="/health",status="200"}') == 0
    )
    assert _value(body, 'driver_tool_calls_total{outcome="success",tool="echo"}') == 0


def test_enabled_metrics_expose_http_labels_and_the_histogram():
    metrics = Metrics()
    metrics.enable()

    metrics.observe_http("GET", "/health", 200, 0.004)

    body = _body(metrics)
    assert (
        _value(body, 'driver_http_requests_total{method="GET",route="/health",status="200"}') == 1
    )
    assert (
        _value(body, 'driver_http_request_duration_seconds_count{method="GET",route="/health"}')
        == 1
    )
    # 状态码是字符串标签：面板上要能按 5xx 过滤，就不能是数字指标名的一部分
    assert 'status="200"' in body
    assert "driver_http_request_duration_seconds_bucket" in body


def test_disable_stops_recording_but_keeps_history():
    """关掉之后不再新增，但已经记下的不消失（Prometheus 侧仍要能抓到关闭前的数）。"""
    metrics = Metrics()
    metrics.enable()
    metrics.observe_tool("echo", OUTCOME_SUCCESS, 0.001)
    metrics.disable()

    metrics.observe_tool("echo", OUTCOME_SUCCESS, 0.001)

    assert _value(_body(metrics), 'driver_tool_calls_total{outcome="success",tool="echo"}') == 1


def test_two_instances_do_not_collide():
    """回归：用默认 registry 时第二个实例会炸 ``Duplicated timeseries``。

    这不是测试的洁癖——多副本、多 app 实例（阶段 8）都会撞上同一件事。
    """
    first = Metrics()
    second = Metrics()

    first.enable()
    second.enable()
    first.observe_tool("echo", OUTCOME_SUCCESS, 0.001)

    assert _value(_body(first), 'driver_tool_calls_total{outcome="success",tool="echo"}') == 1
    assert _body(second).count('driver_tool_calls_total{outcome="success",tool="echo"}') == 0


def test_success_is_recorded_as_the_documented_outcome():
    metrics = Metrics()
    metrics.enable()

    with metrics.track_tool_call("echo"):
        pass

    body = _body(metrics)
    assert _value(body, 'driver_tool_calls_total{outcome="success",tool="echo"}') == 1
    assert _value(body, 'driver_tool_duration_seconds_count{outcome="success",tool="echo"}') == 1


def test_platform_error_becomes_its_own_code():
    """outcome 与对外错误码共用一套词汇：能按 code 分支的地方就能按 outcome 建面板。"""
    metrics = Metrics()
    metrics.enable()

    with (
        pytest.raises(ToolNotFoundError),
        metrics.track_tool_call("ghost", metric_label=UNKNOWN_TOOL_LABEL),
    ):
        raise ToolNotFoundError("未注册的工具：ghost")

    assert (
        _value(_body(metrics), 'driver_tool_calls_total{outcome="tool_not_found",tool="unknown"}')
        == 1
    )


def test_unexpected_exception_is_recorded_and_still_propagates():
    """非预期异常也要记账（用信封里存在的词汇），但绝不能吞掉它。"""
    metrics = Metrics()
    metrics.enable()

    with pytest.raises(RuntimeError), metrics.track_tool_call("echo"):
        raise RuntimeError("内部炸了")

    assert (
        _value(
            _body(metrics),
            f'driver_tool_calls_total{{outcome="{INTERNAL_ERROR_CODE}",tool="echo"}}',
        )
        == 1
    )


def test_disabled_track_is_a_transparent_no_op():
    metrics = Metrics()

    with metrics.track_tool_call("echo"):
        outcome = "ran anyway"

    assert outcome == "ran anyway"
    # ``render()`` 始终带 HELP/TYPE 头，所以「什么都没记」要看**样本行**是否为空
    samples = [line for line in _body(metrics).splitlines() if not line.startswith("#")]
    assert samples == []


def test_buckets_span_milliseconds_to_seconds():
    """桶的边界是契约：默认桶覆盖「本地 SQLite 查询」到「明显卡住」的整个区间。"""
    assert DURATION_BUCKETS == tuple(sorted(DURATION_BUCKETS))
    assert DURATION_BUCKETS[0] == 0.005
    assert DURATION_BUCKETS[-1] == 10.0
