"""MCP stdio 进程入口：``python -m driver_platform.protocols.mcp_server``。

**stdout 是协议通道**：本模块与它调用的代码只把诊断写 stderr。任何一句 ``print()``
都会污染 JSON-RPC 流，客户端看到的就是解析失败而不是错误信息。

凭证来自 ``DRIVER_API_KEY``（**单数**：这个进程只需要自己那一把钥匙），
在**装载插件之前**解析——缺凭证就没必要连数据库。

退出码：``0`` 正常收尾，``2`` 配置/启动期失败（详情见 stderr 上的 JSON 错误信封）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from driver_platform.bootstrap import create_registry_and_host
from driver_platform.config import Settings
from driver_platform.errors import PlatformError
from driver_platform.protocols import require_protocol
from driver_platform.security import ENV_API_KEY, authenticate

MISSING_DEPENDENCY_HINT = 'MCP 适配器需要可选依赖：pip install "driver-platform[mcp]"'
"""没装可选依赖时给人的一句话——首次运行最常见的事故，不该给一段 traceback。"""


async def _serve_configured() -> None:
    # 延迟导入：mcp 是可选依赖，没装时由 main() 的 ImportError 分支说人话。
    from driver_platform.protocols.mcp import serve

    settings = Settings.from_env()
    require_protocol(settings, "mcp")  # 开关默认关：没启用就起不来，而不是半开着
    principal = authenticate(settings, os.environ.get(ENV_API_KEY) or None)
    registry, host = create_registry_and_host(settings)
    try:
        await serve(registry, principal)
    finally:
        host.close()  # 客户端断开时也把插件收尾（SQLite 连接等）


def main() -> int:
    try:
        asyncio.run(_serve_configured())
    except PlatformError as exc:
        print(json.dumps(exc.to_payload(), ensure_ascii=False), file=sys.stderr)
        return 2
    except ImportError as exc:
        print(f"{MISSING_DEPENDENCY_HINT}（{exc}）", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # 客户端关掉了管道
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
