"""HTTP 服务入口：装配注册表、插件宿主、错误处理与路由。"""

from __future__ import annotations

from fastapi import FastAPI

from driver_platform import __version__
from driver_platform.api.plugins import router as plugins_router
from driver_platform.api.tools import router as tools_router
from driver_platform.bootstrap import create_registry_and_host
from driver_platform.config import Settings
from driver_platform.errors import register_error_handlers
from driver_platform.protocols import build_router as build_protocols_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配应用。

    不传 ``settings`` 就读环境变量；测试传 ``Settings(plugins=(), sqlite_path=...)``
    就能精确控制装了什么，不必改进程环境。
    """
    resolved = settings if settings is not None else Settings.from_env()
    registry, host = create_registry_and_host(resolved)

    app = FastAPI(title="Driver Platform", version=__version__)
    app.state.settings = resolved
    app.state.registry = registry
    app.state.plugins = host
    register_error_handlers(app)
    app.include_router(tools_router)
    app.include_router(plugins_router)
    # 协议层的端点是**适配器自己**带进来的：加一家协议不必改这里（原则「适配器隔离」）。
    app.include_router(build_protocols_router())

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
