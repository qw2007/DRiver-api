"""PhoNote 的存储层：一棵自由文件树 + 一个截图缓冲区 + 笔记正文。

只做 P0 需要的事——建库/迁移、树节点、图片去重入库、组笔记、读笔记。
批注、版本、消息、同步都在后面的阶段加表，P0 不预埋空表（`note_get` 因此不返回
``annotations`` / ``revisions`` 字段，而不是返回两个永远是 ``[]`` 的键）。

三条硬约束：
1. 树用**单表自引用**（``parent_id``）：层级深度不设限，改名/移动不需要改任何路径；
2. 图片按 ``sha256`` 内容寻址：同一张图截两次只落一份盘；
3. 所有 SQL 都是静态字符串 + 参数绑定，用户输入永远不进 SQL 文本。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from driver_platform.errors import InvalidArgumentsError, NoteNotFoundError

SCHEMA_VERSION = 1

#: 建库脚本。``user_version`` 是迁移版本号，P1 起每加一版就在这里追加一段。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES nodes (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'note')),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    position INTEGER NOT NULL DEFAULT 0,
    body_md TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (kind = 'note' OR body_md = '')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_nodes_sibling_name
    ON nodes (IFNULL(parent_id, ''), name);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes (parent_id, position);

CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL,
    source TEXT NOT NULL,
    app_title TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_images (
    note_id TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    capture_id TEXT NOT NULL REFERENCES captures (id) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    PRIMARY KEY (note_id, capture_id)
);
"""

#: 认得出的图片格式：魔数 → (媒体类型, 后缀)。壳上传的是 WebP，但 P0 不强制转码。
_MEDIA_TYPES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
)

#: 同层重名的自动后缀上限；超过说明调用方在拿名字当循环变量。
_MAX_NAME_ATTEMPTS = 100

NAME_LIMIT = 200


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _detect_media(data: bytes) -> tuple[str, str]:
    """按魔数判格式。认不出就拒绝——不猜扩展名，免得浏览器渲染成一片空白。"""
    for magic, media_type, suffix in _MEDIA_TYPES:
        if data.startswith(magic):
            return media_type, suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise InvalidArgumentsError("只接受 PNG / JPEG / WebP 图片")


class Store:
    """一个 PhoNote 存储实例 = 一个目录（``phonote.db`` + ``media/``）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "phonote.db"
        self.media_root = self.root / "media"
        # RLock：compose() 会先建节点再挂图，两层都要拿锁（普通 Lock 会自己锁死自己）。
        self._lock = threading.RLock()
        self._connection = self._open()

    # ---------------------------------------------------------------- 生命周期

    def _open(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        # executescript 会先隐式 COMMIT，所以不能套在我们自己的事务里（会报
        # "cannot commit - no transaction is active"）；建表本身是幂等的。
        connection.executescript(_SCHEMA)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
        return connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -------------------------------------------------------------------- 树

    def create_node(
        self,
        *,
        parent_id: str | None,
        kind: str,
        name: str,
        body_md: str = "",
    ) -> dict[str, Any]:
        clean = name.strip()
        if not clean or len(clean) > NAME_LIMIT:
            raise InvalidArgumentsError(f"名字长度必须是 1..{NAME_LIMIT} 个字符")
        if kind not in {"folder", "note"}:
            raise InvalidArgumentsError("kind 只能是 folder 或 note")
        if kind == "folder" and body_md.strip():
            raise InvalidArgumentsError("文件树里的目录不能有正文")
        with self._lock, self._transaction(self._connection) as connection:
            if parent_id is not None and self._folder_is_missing(connection, parent_id):
                raise InvalidArgumentsError(
                    f"父节点不存在或不是目录：{parent_id}",
                    details=[{"type": "unknown_parent", "loc": ["parent_id"], "msg": parent_id}],
                )
            position = self._next_position(connection, parent_id)
            node_id = self._insert_unique(connection, parent_id, kind, clean, position, body_md)
            return self._node_dict(connection, node_id)

    def tree(self, *, include_body: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, parent_id, kind, name, position, body_md FROM nodes "
                "ORDER BY IFNULL(parent_id, ''), position, name"
            ).fetchall()
        by_parent: dict[str | None, list[dict[str, Any]]] = {}
        for row in rows:
            node = {
                "id": row["id"],
                "parent_id": row["parent_id"],
                "kind": row["kind"],
                "name": row["name"],
                "position": row["position"],
                "children": [],
            }
            if include_body and row["kind"] == "note":
                node["body_md"] = row["body_md"]
            by_parent.setdefault(row["parent_id"], []).append(node)
        for children in by_parent.values():
            for child in children:
                child["children"] = by_parent.get(child["id"], [])
        return by_parent.get(None, [])

    # ------------------------------------------------------------------ 图片

    def add_capture(
        self,
        data: bytes,
        *,
        source: str,
        app_title: str | None,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        if not data:
            raise InvalidArgumentsError("图片内容是空的")
        media_type, suffix = _detect_media(data)
        digest = hashlib.sha256(data).hexdigest()
        with self._lock, self._transaction(self._connection) as connection:
            existing = connection.execute(
                "SELECT * FROM captures WHERE sha256 = ?", (digest,)
            ).fetchone()
            if existing is not None:
                return _capture_dict(existing)
            relative = Path("media") / digest[:2] / digest[2:4] / f"{digest}{suffix}"
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_atomically(target, data)
            capture_id = _new_id("cap")
            connection.execute(
                "INSERT INTO captures (id, sha256, path, media_type, width, height, bytes,"
                " source, app_title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    capture_id,
                    digest,
                    relative.as_posix(),
                    media_type,
                    max(width, 0),
                    max(height, 0),
                    len(data),
                    source,
                    app_title,
                    _now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM captures WHERE id = ?", (capture_id,)
            ).fetchone()
            return _capture_dict(row)

    def list_captures(self, *, limit: int, unused_only: bool) -> list[dict[str, Any]]:
        query = "SELECT * FROM captures"
        if unused_only:
            query += " WHERE id NOT IN (SELECT capture_id FROM note_images)"
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._connection.execute(query, (limit,)).fetchall()
        return [_capture_dict(row) for row in rows]

    # ------------------------------------------------------------------ 笔记

    def compose(
        self,
        *,
        parent_id: str | None,
        capture_ids: list[str],
        title: str,
        body_md: str,
    ) -> dict[str, Any]:
        """把缓冲区里的一批图组成为一篇笔记：建节点 + 按顺序挂图，一次事务。"""
        if not capture_ids:
            raise InvalidArgumentsError("至少选一张图片")
        node = self.create_node(parent_id=parent_id, kind="note", name=title, body_md=body_md)
        with self._lock, self._transaction(self._connection) as connection:
            for position, capture_id in enumerate(capture_ids):
                exists = connection.execute(
                    "SELECT 1 FROM captures WHERE id = ?", (capture_id,)
                ).fetchone()
                if exists is None:
                    raise InvalidArgumentsError(
                        f"图片不存在：{capture_id}",
                        details=[
                            {"type": "unknown_capture", "loc": ["capture_ids"], "msg": capture_id}
                        ],
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO note_images (note_id, capture_id, position)"
                    " VALUES (?, ?, ?)",
                    (node["id"], capture_id, position),
                )
        return self.get_note(node["id"])

    def get_note(self, note_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM nodes WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None or row["kind"] != "note":
                raise NoteNotFoundError(
                    f"笔记不存在：{note_id}",
                    details=[{"type": "unknown_note", "loc": ["note_id"], "msg": note_id}],
                )
            images = self._connection.execute(
                "SELECT c.*, ni.position AS position FROM note_images ni"
                " JOIN captures c ON c.id = ni.capture_id"
                " WHERE ni.note_id = ? ORDER BY ni.position",
                (note_id,),
            ).fetchall()
        note = self._node_dict_from_row(row)
        note["images"] = [
            {**_capture_dict(image), "position": image["position"]} for image in images
        ]
        return note

    # ------------------------------------------------------------------ 内部

    def _transaction(self, connection: sqlite3.Connection) -> _Transaction:
        return _Transaction(connection)

    def _folder_is_missing(self, connection: sqlite3.Connection, parent_id: str) -> bool:
        row = connection.execute("SELECT kind FROM nodes WHERE id = ?", (parent_id,)).fetchone()
        return row is None or row["kind"] != "folder"

    def _next_position(self, connection: sqlite3.Connection, parent_id: str | None) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(position), 0) AS top FROM nodes WHERE parent_id IS ?",
            (parent_id,),
        ).fetchone()
        return int(row["top"]) + 1

    def _insert_unique(
        self,
        connection: sqlite3.Connection,
        parent_id: str | None,
        kind: str,
        name: str,
        position: int,
        body_md: str,
    ) -> str:
        """同层重名自动加 `` (2)`` / `` (3)``……最多试 100 次。"""
        for attempt in range(1, _MAX_NAME_ATTEMPTS + 1):
            candidate = name if attempt == 1 else f"{name} ({attempt})"
            node_id = _new_id("nod")
            now = _now()
            try:
                connection.execute(
                    "INSERT INTO nodes (id, parent_id, kind, name, position, body_md,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (node_id, parent_id, kind, candidate, position, body_md, now, now),
                )
            except sqlite3.IntegrityError:
                continue
            return node_id
        raise InvalidArgumentsError(f"同层已经有太多同名节点：{name}")

    def _node_dict(self, connection: sqlite3.Connection, node_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._node_dict_from_row(row)

    @staticmethod
    def _node_dict_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "parent_id": row["parent_id"],
            "kind": row["kind"],
            "name": row["name"],
            "position": row["position"],
            "body_md": row["body_md"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


class _Transaction:
    """``with`` 包一层事务：正常提交，异常回滚（``sqlite3`` 本身不是上下文管理器）。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self._connection.execute("COMMIT")
        else:
            self._connection.execute("ROLLBACK")


def _capture_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "capture_id": row["id"],
        "sha256": row["sha256"],
        "media_type": row["media_type"],
        "width": row["width"],
        "height": row["height"],
        "bytes": row["bytes"],
        "source": row["source"],
        "app_title": row["app_title"],
        "created_at": row["created_at"],
    }


def _write_atomically(target: Path, data: bytes) -> None:
    """先写同目录临时文件再换名：崩溃时不会留下一个「名字合法、内容半截」的图片。"""
    temporary = target.with_name(f".{target.name}.{os.getpid()}.part")
    temporary.write_bytes(data)
    os.replace(temporary, target)
