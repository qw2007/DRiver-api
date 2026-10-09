"""阶段 7/8：演示库种子。

``docker compose up`` 起的驱动平台要能立刻查点东西，所以入口脚本会用本文件先在
``DRIVER_SQLITE_PATH`` 建一份只读可查的 SQLite 库。幂等：文件已存在就直接退出。

**并发安全（阶段 8）**：多副本共挂同一个数据卷时，N 个容器会同时启动并各自看到「库还不存在」。
所以种子不允许直接往目标路径写——先写同目录的临时文件，再用 ``os.replace`` 原子安装。
否则两个副本可能同时往同一个 SQLite 文件写 schema，轻则 "database is locked" 让容器起不来，
重则留下一份半成品库（平台以只读打开，之后谁都查不了）。

只用标准库（容器里也没有别的依赖），schema 与 README 的验收示例一一对应：
``search_database{table="customers", filters={"city": "上海"}}``。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE customers (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    city    TEXT NOT NULL,
    plan    TEXT NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount      REAL NOT NULL,
    created_at  TEXT NOT NULL
);
"""

CUSTOMERS = [
    (1, "Acme 制造", "上海", "pro"),
    (2, "北斗数据", "北京", "pro"),
    (3, "云梯网络", "深圳", "basic"),
    (4, "海纳咨询", "上海", "basic"),
]

ORDERS = [
    (1, 1, 12800.0, "2026-09-01T10:00:00Z"),
    (2, 1, 4300.5, "2026-09-14T09:30:00Z"),
    (3, 2, 9900.0, "2026-09-20T16:45:00Z"),
    (4, 3, 1500.0, "2026-09-28T08:15:00Z"),
]


def _temporary_path(path: Path) -> Path:
    """同目录临时文件：必须与目标同一个文件系统，``os.replace`` 才是原子的。"""
    return path.with_name(f".{path.name}.seed-{os.getpid()}.tmp")


def seed(path: Path) -> bool:
    """建库并写入演示数据；文件已存在时返回 False（幂等）。

    写入是**原子的**：内容先进临时文件，再 ``os.replace`` 就位。中途失败不留半成品，
    并发调用最多让最后完成的那个赢，但目标路径上永远是完整的一份库。
    """
    if path.exists():
        print(f"演示库已存在，跳过种子：{path}")
        return False
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(path)
    try:
        connection = sqlite3.connect(temporary)
        try:
            with connection:
                connection.executescript(SCHEMA)
                connection.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", CUSTOMERS)
                connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ORDERS)
        finally:
            connection.close()
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print(f"演示库已就绪：{path}（customers {len(CUSTOMERS)} 行 / orders {len(ORDERS)} 行）")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建一份驱动平台的演示 SQLite 库")
    parser.add_argument("--path", default="/data/demo.db", help="库文件路径")
    args = parser.parse_args(argv)
    seed(Path(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
