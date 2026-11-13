"""``GET /metrics``：Prometheus 的抓取端点。

默认**不要求凭证**：抓取方是集群内的 Prometheus，给它配一把会轮换的 API Key 只会让人
放弃抓取，最后变成「没人看的指标」。所以边界默认靠**关闭**（开关不开时这个路由根本
不存在，404），要开是显式动作。

需要把它暴露到不可信网络时，设 ``DRIVER_METRICS_PROTECTED=1``：此时端点要走与
``/v1/tools`` 同一套凭证（``Authorization: Bearer`` 或 ``X-API-Key``），Prometheus 的
抓取配置里加 ``authorization`` 即可。注意鉴权关闭（``DRIVER_AUTH_MODE=off``）时
本开关没有意义——没有可校验的凭证，任何调用方都是全权主体。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from driver_platform.observability.metrics import CONTENT_TYPE_LATEST, METRICS, Metrics
from driver_platform.security.http import principal_of


def build_router(metrics: Metrics = METRICS, *, protected: bool = False) -> APIRouter:
    """构造指标路由。

    与协议适配器同样的做法：端点由**能力自己**带进来，``main.py`` 只决定挂不挂——
    这样「指标叫什么、什么格式」只有这一个文件说了算。
    """
    router = APIRouter()
    dependencies = [Depends(principal_of)] if protected else []

    @router.get("/metrics", dependencies=dependencies)
    def metrics_endpoint() -> Response:
        """返回 Prometheus 文本格式快照。"""
        return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return router
