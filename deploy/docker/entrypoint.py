"""阶段 7：容器入口。

做两件事，然后把自己替换成 CMD：

1. 需要时先种一份演示库（``DRIVER_SEED_DEMO_DB=1`` 且 ``DRIVER_SQLITE_PATH`` 还不存在）。
   SQLite 插件是**只读**打开库的（阶段 2 的设计），所以库文件必须先存在——
   这正是「起容器就有一份能查的数据」和「打开一个空目录」之间的差别。
2. ``os.execvp`` 而不是 ``subprocess``：让 uvicorn 成为 PID 1，
   ``docker stop`` 发的 SIGTERM 才能直达应用（否则信号落在 shell/python 上，
   容器要等超时才被杀，滚动更新时就是可见的中断——阶段 8 要验证这一点）。
"""

from __future__ import annotations

import os
import subprocess
import sys

SEED_SCRIPT = "/app/deploy/demo/seed_demo_db.py"
DEFAULT_DB_PATH = "/data/demo.db"
TRUE_WORDS = {"1", "true", "yes", "on"}


def _seed_enabled() -> bool:
    return os.environ.get("DRIVER_SEED_DEMO_DB", "").strip().lower() in TRUE_WORDS


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：entrypoint.py <命令> [参数...]", file=sys.stderr)
        return 2

    db_path = os.environ.get("DRIVER_SQLITE_PATH") or DEFAULT_DB_PATH
    if _seed_enabled() and not os.path.exists(db_path):
        subprocess.run(
            [sys.executable, SEED_SCRIPT, "--path", db_path],
            check=True,
        )
    os.execvp(argv[1], argv[1:])
    return 0  # 不会走到：execvp 成功即替换进程


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
