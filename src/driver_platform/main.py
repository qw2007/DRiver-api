"""HTTP 服务入口：装配注册表、错误处理与路由。"""

from __future__ import annotations

from fastapi import FastAPI

from driver_platform import __version__
from driver_platform.api.tools import router as tools_router
from driver_platform.errors import register_error_handlers
from driver_platform.tools import ToolRegistry, build_default_registry


def create_app(registry: ToolRegistry | None = None) -> FastAPI:
    """装配应用。

    传入 ``registry`` 即可整体替换工具表——测试用临时工具、阶段 2 的插件宿主
    都走这个口子，不需要改路由。
    """
    app = FastAPI(title="Driver Platform", version=__version__)
    app.state.registry = registry if registry is not None else build_default_registry()
    register_error_handlers(app)
    app.include_router(tools_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
