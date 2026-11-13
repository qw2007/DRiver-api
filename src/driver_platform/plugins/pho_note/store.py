"""PhoNote 的存储层：一棵自由文件树 + 一个截图缓冲区 + 笔记正文 + 批注 + 版本。

P0 只做「截图进缓冲区 → 组笔记 → 读回来」；P1 补齐树的全量增删改移、批注与版本，
并给壳一层 REST。消息（讨论区）与同步仍在后面的阶段加表，这里不预埋空表。

三条硬约束：
1. 树用**单表自引用**（``parent_id``）：层级深度不设限，改名/移动不需要改任何路径；
2. 图片按 ``sha256`` 内容寻址：同一张图截两次只落一份盘；
3. 所有 SQL 都是静态字符串 + 参数绑定，用户输入永远不进 SQL 文本。

迁移方式：每个版本的新表都用 ``CREATE TABLE IF NOT EXISTS`` 写进同一个脚本，
``PRAGMA user_version`` 只是标记——P0 的库（v1）打开后会多出批注与版本两张表，
不需要另写一段 ALTER。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from driver_platform.errors import (
    InvalidArgumentsError,
    NodeMoveInvalidError,
    NodeNameConflictError,
    NoteNotFoundError,
)

SCHEMA_VERSION = 3

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

CREATE TABLE IF NOT EXISTS annotations (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('note', 'text', 'image')),
    anchor TEXT NOT NULL DEFAULT '{}',
    body_md TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT 'yellow',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotations_note ON annotations (note_id, created_at);

CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    body_md TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_note ON revisions (note_id, created_at);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    body TEXT NOT NULL,
    revision_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_note ON messages (note_id, created_at);
"""

#: 认得出的图片格式：魔数 → (媒体类型, 后缀)。壳上传的是 WebP，但 P0 不强制转码。
_MEDIA_TYPES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
)

#: 同层重名的自动后缀上限；超过说明调用方在拿名字当循环变量。
_MAX_NAME_ATTEMPTS = 100

NAME_LIMIT = 200

#: 新建笔记时可选套用的骨架。只有一份内置模板：网课笔记最常见的就是「课上抄定义、
#: 记定理、标易错」，先给这三栏比给十份花模板更有用；用户不选模板时正文保持空。
TEMPLATES: dict[str, str] = {
    "lecture": ("## 定义\n\n\n## 定理 / 结论\n\n\n## 易错点\n\n\n## 例题\n")
}

TEMPLATE_NAMES = tuple(TEMPLATES)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _stamp() -> str:
    """文件名用的时间戳（同一秒内备份两次会撞名，但覆盖自己对备份场景无害）。"""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


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
        template: str | None = None,
    ) -> dict[str, Any]:
        clean = _clean_name(name)
        if kind not in {"folder", "note"}:
            raise InvalidArgumentsError("kind 只能是 folder 或 note")
        if kind == "folder" and body_md.strip():
            raise InvalidArgumentsError("文件树里的目录不能有正文")
        if template is not None:
            if kind != "note":
                raise InvalidArgumentsError("模板只能套在笔记上，目录没有正文")
            if template not in TEMPLATES:
                raise InvalidArgumentsError(
                    f"未知模板：{template}",
                    details=[
                        {
                            "type": "unknown_template",
                            "loc": ["template"],
                            "msg": ", ".join(TEMPLATE_NAMES),
                        }
                    ],
                )
            # 显式给了正文就以正文为准：模板只是「新建时替你写好骨架」，不是覆盖。
            if not body_md.strip():
                body_md = TEMPLATES[template]
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

    def get_capture(self, capture_id: str) -> dict[str, Any]:
        """按 id 取一张图（含相对路径），给 REST 的图片端点用。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM captures WHERE id = ?", (capture_id,)
            ).fetchone()
        if row is None:
            raise InvalidArgumentsError(
                f"图片不存在：{capture_id}",
                details=[{"type": "unknown_capture", "loc": ["capture_id"], "msg": capture_id}],
            )
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
            annotations = self._connection.execute(
                "SELECT * FROM annotations WHERE note_id = ? ORDER BY created_at",
                (note_id,),
            ).fetchall()
        note = self._node_dict_from_row(row)
        note["images"] = [
            {**_capture_dict(image), "position": image["position"]} for image in images
        ]
        note["annotations"] = [self._annotation_dict(item) for item in annotations]
        return note

    # ------------------------------------------------------------- 树：增删改移

    def get_node(self, node_id: str) -> dict[str, Any]:
        with self._lock, self._transaction(self._connection) as connection:
            return self._node_dict(connection, self._node_row(connection, node_id)["id"])

    def rename_node(self, node_id: str, name: str) -> dict[str, Any]:
        clean = _clean_name(name)
        with self._lock, self._transaction(self._connection) as connection:
            row = self._node_row(connection, node_id)
            try:
                connection.execute(
                    "UPDATE nodes SET name = ?, updated_at = ? WHERE id = ?",
                    (clean, _now(), row["id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise NodeNameConflictError(
                    f"同层已经有叫「{clean}」的节点",
                    details=[{"type": "duplicate_name", "loc": ["name"], "msg": clean}],
                ) from exc
            return self._node_dict(connection, node_id)

    def move_node(
        self,
        node_id: str,
        *,
        parent_id: str | None,
        position: int | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._transaction(self._connection) as connection:
            row = self._node_row(connection, node_id)
            if parent_id is not None:
                parent = connection.execute(
                    "SELECT kind FROM nodes WHERE id = ?", (parent_id,)
                ).fetchone()
                if parent is None or parent["kind"] != "folder":
                    raise NodeMoveInvalidError(f"目标父节点不存在或不是目录：{parent_id}")
                # 环形检查：目标在「自己这棵子树」里（含自己）就会把树撕成两半。
                if parent_id == node_id or self._is_descendant(connection, node_id, parent_id):
                    raise NodeMoveInvalidError("不能把节点移动到它自己或它的子节点下")
            target = (
                position if position is not None else self._next_position(connection, parent_id)
            )
            try:
                connection.execute(
                    "UPDATE nodes SET parent_id = ?, position = ?, updated_at = ? WHERE id = ?",
                    (parent_id, target, _now(), row["id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise NodeNameConflictError(
                    f"目标目录下已经有叫「{row['name']}」的节点",
                    details=[{"type": "duplicate_name", "loc": ["name"], "msg": row["name"]}],
                ) from exc
            return self._node_dict(connection, node_id)

    def delete_node(self, node_id: str, *, recursive: bool) -> dict[str, Any]:
        with self._lock, self._transaction(self._connection) as connection:
            self._node_row(connection, node_id)
            size = self._subtree_size(connection, node_id)
            if size > 1 and not recursive:
                raise InvalidArgumentsError(
                    f"节点下还有 {size - 1} 个子孙；确认要一起删就传 recursive=true"
                )
            # 子孙由 ON DELETE CASCADE 带走；图片留在缓冲区（可能被别的笔记用着）。
            connection.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            return {"deleted": size, "node_id": node_id}

    # ------------------------------------------------------------- 笔记

    def update_note(
        self,
        note_id: str,
        *,
        name: str | None = None,
        body_md: str | None = None,
        reason: str = "edit",
    ) -> dict[str, Any]:
        with self._lock, self._transaction(self._connection) as connection:
            row = self._node_row(connection, note_id)
            if row["kind"] != "note":
                raise InvalidArgumentsError("这是目录，没有正文可改")
            new_name = row["name"] if name is None else _clean_name(name)
            new_body = row["body_md"] if body_md is None else body_md
            if body_md is not None and new_body != row["body_md"]:
                # 旧正文先存档再覆盖：撤回一步的成本，换「改坏了还能退回来」。
                self._add_revision(connection, note_id, row["body_md"], reason)
            try:
                connection.execute(
                    "UPDATE nodes SET name = ?, body_md = ?, updated_at = ? WHERE id = ?",
                    (new_name, new_body, _now(), row["id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise NodeNameConflictError(
                    f"同层已经有叫「{new_name}」的节点",
                    details=[{"type": "duplicate_name", "loc": ["name"], "msg": new_name}],
                ) from exc
            return self._node_dict(connection, note_id)

    def list_notes(self, *, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """按名字或正文搜笔记。``query`` 里的 ``%`` / ``_`` 当普通字符（ESCAPE '\\'）。"""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, parent_id, name, updated_at, substr(body_md, 1, 200) AS snippet"
                " FROM nodes WHERE kind = 'note'"
                " AND (name LIKE ? ESCAPE '\\' OR body_md LIKE ? ESCAPE '\\')"
                " ORDER BY updated_at DESC LIMIT ?",
                (pattern, pattern, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_images(self, note_id: str, capture_ids: list[str]) -> dict[str, Any]:
        """用给定顺序整体重写一篇笔记的图片列表（增、删、重排都走这一条路径）。"""
        with self._lock, self._transaction(self._connection) as connection:
            row = self._node_row(connection, note_id)
            if row["kind"] != "note":
                raise InvalidArgumentsError("目录不能挂图片")
            for capture_id in capture_ids:
                if (
                    connection.execute(
                        "SELECT 1 FROM captures WHERE id = ?", (capture_id,)
                    ).fetchone()
                    is None
                ):
                    raise InvalidArgumentsError(
                        f"图片不存在：{capture_id}",
                        details=[
                            {"type": "unknown_capture", "loc": ["capture_ids"], "msg": capture_id}
                        ],
                    )
            connection.execute("DELETE FROM note_images WHERE note_id = ?", (note_id,))
            for position, capture_id in enumerate(capture_ids):
                connection.execute(
                    "INSERT INTO note_images (note_id, capture_id, position) VALUES (?, ?, ?)",
                    (note_id, capture_id, position),
                )
        return self.get_note(note_id)

    # ------------------------------------------------------------- 批注

    def add_annotation(
        self,
        note_id: str,
        *,
        kind: str,
        anchor: dict[str, Any],
        body_md: str,
        color: str = "yellow",
    ) -> dict[str, Any]:
        if kind not in {"note", "text", "image"}:
            raise InvalidArgumentsError("批注类型只能是 note / text / image")
        text = body_md.strip()
        if not text:
            raise InvalidArgumentsError("批注内容不能为空")
        with self._lock, self._transaction(self._connection) as connection:
            row = self._node_row(connection, note_id)
            if row["kind"] != "note":
                raise InvalidArgumentsError("只能给笔记加批注")
            annotation_id = _new_id("ann")
            connection.execute(
                "INSERT INTO annotations (id, note_id, kind, anchor, body_md, color, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    annotation_id,
                    note_id,
                    kind,
                    json.dumps(anchor, ensure_ascii=False),
                    text,
                    color,
                    _now(),
                ),
            )
            return self._annotation_dict(
                connection.execute(
                    "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
                ).fetchone()
            )

    def delete_annotation(self, annotation_id: str) -> dict[str, Any]:
        with self._lock, self._transaction(self._connection) as connection:
            row = connection.execute(
                "SELECT id FROM annotations WHERE id = ?", (annotation_id,)
            ).fetchone()
            if row is None:
                raise InvalidArgumentsError(f"批注不存在：{annotation_id}")
            connection.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
            return {"deleted": 1, "annotation_id": annotation_id}

    # ------------------------------------------------------------- 版本

    def list_revisions(self, note_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM revisions WHERE note_id = ? ORDER BY created_at DESC LIMIT ?",
                (note_id, limit),
            ).fetchall()
        return [_revision_dict(row) for row in rows]

    def rollback(self, note_id: str, revision_id: str) -> dict[str, Any]:
        with self._lock, self._transaction(self._connection) as connection:
            row = self._node_row(connection, note_id)
            if row["kind"] != "note":
                raise InvalidArgumentsError("只能回滚笔记")
            target = connection.execute(
                "SELECT * FROM revisions WHERE id = ? AND note_id = ?", (revision_id, note_id)
            ).fetchone()
            if target is None:
                raise InvalidArgumentsError(f"这篇笔记没有这个版本：{revision_id}")
            # 回滚本身也是一次修改：先把当前正文存档，撤回的动作可再被撤回。
            self._add_revision(connection, note_id, row["body_md"], f"rollback:{revision_id}")
            connection.execute(
                "UPDATE nodes SET body_md = ?, updated_at = ? WHERE id = ?",
                (target["body_md"], _now(), note_id),
            )
        return self.get_note(note_id)

    # ------------------------------------------------------------- 讨论消息

    def add_message(
        self,
        note_id: str,
        *,
        role: str,
        body: str,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        """往笔记的讨论区追加一条消息（user 提问 / assistant 回复）。"""

        if role not in {"user", "assistant"}:
            raise InvalidArgumentsError(f"role 只能是 user 或 assistant，收到：{role!r}")
        with self._lock, self._transaction(self._connection) as connection:
            self._node_row(connection, note_id)
            message_id = _new_id("msg")
            connection.execute(
                "INSERT INTO messages (id, note_id, role, body, revision_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, note_id, role, body, revision_id, _now()),
            )
        return {
            "message_id": message_id,
            "note_id": note_id,
            "role": role,
            "body": body,
            "revision_id": revision_id,
        }

    def list_messages(self, note_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            self._node_row(self._connection, note_id)
            rows = self._connection.execute(
                "SELECT * FROM messages WHERE note_id = ? ORDER BY created_at ASC, rowid ASC "
                "LIMIT ?",
                (note_id, limit),
            ).fetchall()
        return [_message_dict(row) for row in rows]

    # ------------------------------------------------------------- 媒体字节

    def media_bytes(self, capture_id: str) -> tuple[str, bytes]:
        """给 AI 附图用：返回 (媒体类型, 原字节)。"""

        capture = self.get_capture(capture_id)
        target = self.root / capture["path"]
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise NoteNotFoundError(f"图片文件丢失：{capture['path']}") from exc
        return capture["media_type"], data

    # ------------------------------------------------------------- 备份与同步

    def reload(self) -> None:
        """关掉再打开连接（pull 覆盖了库文件之后必须走这一步）。"""

        with self._lock:
            self._connection.close()
            self._connection = self._open()

    def snapshot_bytes(self) -> bytes:
        """``VACUUM INTO`` 出一份一致的库文件副本（WAL 模式下直接拷 .db 会缺内容）。"""

        with self._lock:
            target = self.root / f".snapshot-{os.getpid()}.db"
            target.unlink(missing_ok=True)
            try:
                self._connection.execute("VACUUM INTO ?", (str(target),))
                return target.read_bytes()
            finally:
                target.unlink(missing_ok=True)

    def media_files(self) -> list[dict[str, Any]]:
        """所有图片的（sha256, 相对路径, 媒体类型），同步时按 sha 去重。"""

        with self._lock:
            rows = self._connection.execute(
                "SELECT sha256, path, media_type FROM captures ORDER BY sha256"
            ).fetchall()
        return [
            {"sha256": row["sha256"], "path": row["path"], "media_type": row["media_type"]}
            for row in rows
        ]

    def fingerprint(self) -> str:
        """内容指纹：用「行数 + 最近时间」而不是文件字节数。

        ``VACUUM INTO`` 出来的快照与活动库文件**字节不同**（页布局重排），
        所以判「本地有没有未推送改动」不能比文件 sha256；这里比的是内容摘要，
        VACUUM、重开进程、WAL checkpoint 都不影响它，任何一次写都会变。
        """

        with self._lock:
            row = self._connection.execute(
                """
                SELECT (SELECT COUNT(*) FROM nodes),
                       (SELECT COALESCE(MAX(updated_at), '') FROM nodes),
                       (SELECT COUNT(*) FROM captures),
                       (SELECT COUNT(*) FROM revisions),
                       (SELECT COUNT(*) FROM annotations),
                       (SELECT COUNT(*) FROM messages)
                """
            ).fetchone()
        joined = "|".join(str(value) for value in row)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def backup_db(self) -> Path:
        """同步前先把当前库落一份到 ``backups/``（pull 覆盖失败时还能捡回来）。"""

        target = self.root / "backups" / f"pre-pull-{_stamp()}.db"
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(target, self.snapshot_bytes())
        return target

    def backup_zip(self) -> dict[str, Any]:
        """把库快照和全部图片打成一个 zip（换机器/离线恢复用）。"""

        target = self.root / "backups" / f"backup-{_stamp()}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot_bytes()
        written = 0
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("phonote.db", snapshot)
            for item in self.media_files():
                source = self.root / item["path"]
                if not source.is_file():
                    continue
                archive.write(source, item["path"])
                written += 1
        return {
            "path": str(target),
            "bytes": target.stat().st_size,
            "media_files": written,
        }

    # ------------------------------------------------------------- 导出

    def export_markdown(self, note_id: str) -> dict[str, Any]:
        """把笔记渲成一份自带图片绝对路径的 Markdown（粘到哪里图都在）。"""
        note = self.get_note(note_id)
        lines = [f"# {note['name']}", ""]
        if note["body_md"].strip():
            lines += [note["body_md"].rstrip(), ""]
        images = []
        for image in note["images"]:
            path = (self.root / image["path"]).resolve()
            caption = image.get("app_title") or image["capture_id"]
            lines.append(f"![{caption}](<{path.as_posix()}>)")
            images.append({"capture_id": image["capture_id"], "path": path.as_posix()})
        markdown = "\n".join(lines).rstrip() + "\n"
        return {
            "note_id": note_id,
            "name": note["name"],
            "markdown": markdown,
            "images": images,
        }

    # ------------------------------------------------------------------ 内部

    def _node_row(self, connection: sqlite3.Connection, node_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            raise NoteNotFoundError(
                f"节点不存在：{node_id}",
                details=[{"type": "unknown_node", "loc": ["node_id"], "msg": node_id}],
            )
        return row

    def _is_descendant(
        self, connection: sqlite3.Connection, ancestor_id: str, candidate_id: str
    ) -> bool:
        row = connection.execute(
            "WITH RECURSIVE sub (id) AS ("
            "  SELECT id FROM nodes WHERE id = ?"
            "  UNION ALL"
            "  SELECT n.id FROM nodes n JOIN sub ON n.parent_id = sub.id"
            ") SELECT 1 FROM sub WHERE id = ? LIMIT 1",
            (ancestor_id, candidate_id),
        ).fetchone()
        return row is not None

    def _subtree_size(self, connection: sqlite3.Connection, node_id: str) -> int:
        row = connection.execute(
            "WITH RECURSIVE sub (id) AS ("
            "  SELECT id FROM nodes WHERE id = ?"
            "  UNION ALL"
            "  SELECT n.id FROM nodes n JOIN sub ON n.parent_id = sub.id"
            ") SELECT COUNT(*) AS total FROM sub",
            (node_id,),
        ).fetchone()
        return int(row["total"])

    def _add_revision(
        self, connection: sqlite3.Connection, note_id: str, body_md: str, reason: str
    ) -> None:
        connection.execute(
            "INSERT INTO revisions (id, note_id, body_md, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (_new_id("rev"), note_id, body_md, reason, _now()),
        )

    @staticmethod
    def _annotation_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "annotation_id": row["id"],
            "note_id": row["note_id"],
            "kind": row["kind"],
            "anchor": json.loads(row["anchor"]),
            "body_md": row["body_md"],
            "color": row["color"],
            "created_at": row["created_at"],
        }

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
        "path": row["path"],
    }


def _clean_name(name: str) -> str:
    clean = name.strip()
    if not clean or len(clean) > NAME_LIMIT:
        raise InvalidArgumentsError(f"名字长度必须是 1..{NAME_LIMIT} 个字符")
    return clean


def _revision_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "revision_id": row["id"],
        "note_id": row["note_id"],
        "body_md": row["body_md"],
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "message_id": row["id"],
        "note_id": row["note_id"],
        "role": row["role"],
        "body": row["body"],
        "revision_id": row["revision_id"],
        "created_at": row["created_at"],
    }


def _write_atomically(target: Path, data: bytes) -> None:
    """先写同目录临时文件再换名：崩溃时不会留下一个「名字合法、内容半截」的图片。"""
    temporary = target.with_name(f".{target.name}.{os.getpid()}.part")
    temporary.write_bytes(data)
    os.replace(temporary, target)
