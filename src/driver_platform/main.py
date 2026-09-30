"""HTTP 服务入口。

阶段 0 只暴露健康检查，用来验证「服务能起、CI 能跑」这条链路。
"""

from fastapi import FastAPI

from driver_platform import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="Driver Platform", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
