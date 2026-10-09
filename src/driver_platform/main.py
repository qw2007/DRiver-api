"""HTTP 服务入口：装配注册表、插件宿主、错误处理与路由。"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from driver_platform import __version__
from driver_platform.api.plugins import router as plugins_router
from driver_platform.api.tools import router as tools_router
from driver_platform.bootstrap import create_registry_and_host
from driver_platform.config import Settings
from driver_platform.errors import register_error_handlers
from driver_platform.observability.logging import configure_logging
from driver_platform.observability.metrics import METRICS
from driver_platform.observability.middleware import ObservabilityMiddleware
from driver_platform.observability.routes import build_router as build_metrics_router
from driver_platform.plugins.host import PluginHost
from driver_platform.protocols import build_router as build_protocols_router
from driver_platform.security import AUTH_MODE_OFF, ENV_AUTH_MODE

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程收尾（阶段 8）。

    ``docker stop`` 的 SIGTERM 直达 uvicorn（阶段 7 的入口脚本用 ``os.execvp``，uvicorn 就是 PID 1），
    「停止接受新连接 + 等在途请求收尾」由 uvicorn 的 ``--timeout-graceful-shutdown`` 负责；
    这里只负责**关掉插件资源**（SQLite 连接、将来的连接池）——否则每次滚动替换都会漏掉一份。
    """
    yield
    host: PluginHost = app.state.plugins
    logger.info("进程收尾：卸载插件（逆序，逐个尽力而为）")
    host.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配应用。

    不传 ``settings`` 就读环境变量；测试传 ``Settings(plugins=(), sqlite_path=...)``
    就能精确控制装了什么，不必改进程环境。
    """
    resolved = settings if settings is not None else Settings.from_env()
    # 日志先于其它一切都装配：后面任何一步失败（装插件、连库、配置错误）都得有人看得见。
    configure_logging(resolved.log_level, resolved.log_format)
    registry, host = create_registry_and_host(resolved)

    # 指标开关是**进程级**的：Prometheus 抓的就是这个进程的数字，所以按配置打开/关掉
    # 默认指标集，而不是给每个 app 造一份（否则同一进程里两个 app 会互相覆盖）。
    if resolved.metrics_enabled:
        METRICS.enable()
    else:
        METRICS.disable()

    if resolved.auth_mode == AUTH_MODE_OFF:
        # 开放模式是**说出来的**，不是默认出来的：静默开放会让人以为门已经锁上了。
        logger.warning(
            "鉴权未启用（%s=%s）：任何能连上该端口的调用方都能使用全部工具",
            ENV_AUTH_MODE,
            AUTH_MODE_OFF,
        )

    app = FastAPI(title="Driver Platform", version=__version__, lifespan=_lifespan)
    app.state.settings = resolved
    app.state.registry = registry
    app.state.plugins = host
    # 可观测性中间件盖在最外层：404、401、校验失败这些「没进业务代码」的请求也要发号与记账。
    app.add_middleware(ObservabilityMiddleware)
    register_error_handlers(app)
    app.include_router(tools_router)
    app.include_router(plugins_router)
    # 协议层的端点是**适配器自己**带进来的：加一家协议不必改这里（原则「适配器隔离」）。
    app.include_router(build_protocols_router())
    if resolved.metrics_enabled:
        # 端点也由能力自己带进来，挂不挂由开关决定——关掉时 /metrics 根本不存在（404）。
        app.include_router(build_metrics_router())

    @app.get("/health")
    def health() -> dict[str, str]:
        """探针端点是唯一公开的端点：门禁不该把监控一起挡在门外。"""
        return {"status": "ok"}

    return app


app = create_app()
