"""插件子系统：契约（base）、宿主（host）、目录（catalog）、具体插件。"""

from __future__ import annotations

from driver_platform.plugins.base import (
    PLUGIN_ID_PATTERN,
    PLUGIN_VERSION_PATTERN,
    Plugin,
    PluginManifest,
    PluginToolSummary,
)
from driver_platform.plugins.catalog import PLUGIN_FACTORIES, available_plugins, build_plugin
from driver_platform.plugins.host import PluginHost
from driver_platform.plugins.sqlite import SqlitePlugin

__all__ = [
    "PLUGIN_FACTORIES",
    "PLUGIN_ID_PATTERN",
    "PLUGIN_VERSION_PATTERN",
    "Plugin",
    "PluginHost",
    "PluginManifest",
    "PluginToolSummary",
    "SqlitePlugin",
    "available_plugins",
    "build_plugin",
]
