"""装配：把配置、内置工具、插件宿主串成一个可运行的运行时。

放在这里而不是 ``main.py``，是为了让「怎么装配」与「怎么暴露成 HTTP」分开：
测试可以直接调 ``create_registry_and_host()`` 拿到装配好的运行时，不起服务。
"""

from __future__ import annotations

from driver_platform.config import Settings
from driver_platform.plugins.catalog import build_plugin
from driver_platform.plugins.host import PluginHost
from driver_platform.plugins.remote import RemotePlugin
from driver_platform.tools import ToolRegistry, register_builtin_tools


def create_registry_and_host(settings: Settings) -> tuple[ToolRegistry, PluginHost]:
    """按配置装配注册表与插件宿主。

    插件在启动期同步装载：配置写错（未知 id、缺库路径、工具名冲突）就让进程起不来，
    而不是等到某个请求打进来才 500。

    远程插件同样在启动期装载，因此**它连不上时进程也起不来**——这是刻意的：
    「启动即失败」比「跑起来一半工具是坏的」容易排查得多（不重试、不降级，
    见阶段 6 的非目标）。
    """
    registry = ToolRegistry()
    register_builtin_tools(registry)
    host = PluginHost(registry)
    for plugin_id in settings.plugins:
        host.load(build_plugin(plugin_id, settings))
    for plugin_id, base_url in settings.remote_plugins:
        host.load(RemotePlugin(plugin_id, base_url, timeout=settings.remote_timeout))
    return registry, host
