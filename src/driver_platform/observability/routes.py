"""``GET /metrics``：Prometheus 的抓取端点。

与 ``/health`` 一样**不要求凭证**：抓取方是集群内的 Prometheus，给它配一把会轮换的
API Key 只会让人放弃抓取，最后变成「没人看的指标」。边界不靠鉴权，靠**默认关闭**：
开关不开时这个路由根本不存在（404），要开是显式动作，且文档明说「开了就是公开的」。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from driver_platform.observability.metrics import CONTENT_TYPE_LATEST, METRICS, Metrics


def build_router(metrics: Metrics = METRICS) -> APIRouter:
    """构造指标路由。

    与协议适配器同样的做法：端点由**能力自己**带进来，``main.py`` 只决定挂不挂——
    这样「指标叫什么、什么格式」只有这一个文件说了算。
    """
    router = APIRouter()

    @router.get("/metrics")
    def metrics_endpoint() -> Response:
        """返回 Prometheus 文本格式快照。"""
        return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return router
