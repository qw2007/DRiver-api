"""阶段 8：进程生命周期契约——收到停机信号时把插件资源还回去。

``create_app()`` 现在带 FastAPI ``lifespan``：uvicorn 收到 SIGTERM 后先停收新连接、等在途请求
收尾（``--timeout-graceful-shutdown 20``），最后跑 lifespan 的退出阶段——那里调
``PluginHost.close()`` 逆序卸载每个插件。缺了这段，容器被替换时插件的连接与临时文件
只能等进程被 SIGKILL 后由内核回收。

**只有 ``with TestClient(app)``（或真的 uvicorn）才触发 lifespan**，直接用 ``TestClient(app)``
不触发——``tests/conftest.py`` 的夹具正是后者（阶段 1 起的用例只关心请求，不需要每个用例
都跑一遍收尾）。所以这段代码的覆盖点集中在这个文件里，别把它搬进夹具。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.main import create_app
from driver_platform.plugins.base import Plugin
from driver_platform.tools.base import Tool


def test_shutdown_unloads_every_plugin_and_tool(demo_db: str) -> None:
    app = create_app(Settings(plugins=("sqlite",), sqlite_path=demo_db))

    with TestClient(app) as client:
        names = {manifest["name"] for manifest in client.get("/v1/tools").json()}
        assert "search_database" in names
        # 运行中：注册表与插件宿主都是有东西的。
        assert len(app.state.registry) == len(names) > 0
        assert len(app.state.plugins) == 1

    # 已退出：插件卸载，它注册的工具也跟着消失（没有悬空的工具指向已关闭的库）；
    # 但内置工具不是插件的，不能被顺手摘掉。
    assert len(app.state.plugins) == 0
    assert set(app.state.registry.names()) == names - {"search_database"}


def test_shutdown_is_best_effort_when_one_teardown_raises(demo_db: str) -> None:
    """一个插件的 ``teardown()`` 炸了，不能把进程退出卡住，也不能连累其它插件。"""

    class Exploding(Plugin):
        plugin_id = "exploding"
        version = "1.0.0"
        description = "teardown 必炸（测试用）"

        def tools(self) -> list[Tool[Any, Any]]:
            return []

        def teardown(self) -> None:
            raise RuntimeError("teardown 炸了")

    app = create_app(Settings(plugins=("sqlite",), sqlite_path=demo_db))

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        builtin = set(app.state.registry.names())
        assert "search_database" in builtin
        app.state.plugins.load(Exploding())
        assert len(app.state.plugins) == 2

    # 退出阶段没有向外抛异常（抛了 TestClient 会带着它退出），两个插件都被卸载；
    # 插件注册的工具随插件一起摘掉，内置工具留在原地。
    assert len(app.state.plugins) == 0
    assert set(app.state.registry.names()) == builtin - {"search_database"}
