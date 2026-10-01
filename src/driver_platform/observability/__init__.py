"""可观测性：指标、结构化日志、请求相关性。

**这个包的 ``__init__`` 刻意不做 re-export**（与其他包的风格不同，理由写在下面）。

``errors.py`` 需要 request_id 才能把它写进错误信封，而 ``observability/metrics.py``
又需要 ``errors.PlatformError`` 才能把异常翻译成 outcome 标签——如果本 ``__init__``
把 ``metrics`` 拉进来，两条 import 链就会在启动时撞成循环（``errors`` 还没定义完
``PlatformError``，``metrics`` 就来取了）。所以这里只留一个 docstring，调用方显式
import 子模块：``from driver_platform.observability.context import current_request_id``。
剥掉这一层便利，换来的是「谁依赖谁」在 import 语句里一目了然。

模块职责：

===================  ==========================================================
``context``          request_id 的 ContextVar（零依赖，最低层）
``metrics``          Prometheus 指标 + 工具调用埋点（不认识 FastAPI）
``logging``          JSON / key=value 两种格式与 ``configure_logging()``
``middleware``       纯 ASGI 中间件：响应头、访问日志、HTTP 指标
``routes``           ``GET /metrics``（仅开关打开时被 ``main.py`` 挂载）
===================  ==========================================================
"""
