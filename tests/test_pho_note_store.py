"""PhoNote 存储层：树、图片缓冲区、笔记组装。

这些用例全部落在 ``tmp_path`` 下的真实 SQLite 上——存储层没有 mock，
因为这里要证明的正是「换个进程打开，数据还在」。
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from driver_platform.errors import InvalidArgumentsError, NoteNotFoundError
from driver_platform.plugins.pho_note.store import SCHEMA_VERSION, Store

#: 1x1 透明 PNG（真实字节，足以验证魔数识别与落盘）。
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 8


@pytest.fixture
def store(tmp_path: Path) -> Store:
    instance = Store(tmp_path / "notes")
    yield instance
    instance.close()


def test_creates_database_and_media_directory(tmp_path: Path) -> None:
    instance = Store(tmp_path / "notes")
    try:
        assert (tmp_path / "notes" / "phonote.db").is_file()
        assert (tmp_path / "notes" / "media").is_dir()
        version = instance._connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        instance.close()


def test_tree_starts_empty(store: Store) -> None:
    assert store.tree() == []


def test_nested_folder_and_note(store: Store) -> None:
    course = store.create_node(parent_id=None, kind="folder", name="线性代数")
    chapter = store.create_node(parent_id=course["id"], kind="folder", name="第 1 讲")
    note = store.create_node(
        parent_id=chapter["id"], kind="note", name="矩阵", body_md="# 矩阵\n\n定义……"
    )

    tree = store.tree()
    assert [node["name"] for node in tree] == ["线性代数"]
    chapters = tree[0]["children"]
    assert [node["name"] for node in chapters] == ["第 1 讲"]
    assert [node["name"] for node in chapters[0]["children"]] == ["矩阵"]
    assert store.get_note(note["id"])["body_md"] == "# 矩阵\n\n定义……"


def test_sibling_names_get_a_suffix(store: Store) -> None:
    first = store.create_node(parent_id=None, kind="folder", name="课程")
    second = store.create_node(parent_id=None, kind="folder", name="课程")
    third = store.create_node(parent_id=None, kind="folder", name="课程")

    assert [first["name"], second["name"], third["name"]] == ["课程", "课程 (2)", "课程 (3)"]


def test_same_name_is_fine_under_different_parents(store: Store) -> None:
    left = store.create_node(parent_id=None, kind="folder", name="A")
    right = store.create_node(parent_id=None, kind="folder", name="B")

    here = store.create_node(parent_id=left["id"], kind="folder", name="复习")
    there = store.create_node(parent_id=right["id"], kind="folder", name="复习")

    assert here["name"] == there["name"] == "复习"


def test_parent_must_be_a_folder(store: Store) -> None:
    note = store.create_node(parent_id=None, kind="note", name="笔记")

    with pytest.raises(InvalidArgumentsError):
        store.create_node(parent_id=note["id"], kind="folder", name="子目录")

    with pytest.raises(InvalidArgumentsError):
        store.create_node(parent_id="nod_missing", kind="folder", name="子目录")


def test_folder_cannot_carry_body(store: Store) -> None:
    with pytest.raises(InvalidArgumentsError):
        store.create_node(parent_id=None, kind="folder", name="目录", body_md="正文")

    with pytest.raises(InvalidArgumentsError):
        store.create_node(parent_id=None, kind="folder", name="   ")


def test_add_capture_writes_the_file_and_returns_metadata(store: Store) -> None:
    capture = store.add_capture(PNG, source="shell", app_title="网课 · 第 1 讲", width=1, height=1)

    assert capture["media_type"] == "image/png"
    assert capture["bytes"] == len(PNG)
    assert capture["width"] == 1 and capture["height"] == 1
    written = store.root / "media" / capture["sha256"][:2] / capture["sha256"][2:4]
    assert (written / f"{capture['sha256']}.png").read_bytes() == PNG


@pytest.mark.parametrize("data,media_type", [(JPEG, "image/jpeg"), (WEBP, "image/webp")])
def test_recognizes_jpeg_and_webp(store: Store, data: bytes, media_type: str) -> None:
    assert (
        store.add_capture(data, source="shell", app_title=None, width=0, height=0)["media_type"]
        == media_type
    )


def test_same_image_twice_is_stored_once(store: Store) -> None:
    first = store.add_capture(PNG, source="shell", app_title=None, width=1, height=1)
    second = store.add_capture(PNG, source="shell", app_title=None, width=1, height=1)

    assert first["capture_id"] == second["capture_id"]
    files = [path for path in (store.root / "media").rglob("*") if path.is_file()]
    assert len(files) == 1


def test_rejects_unknown_image_format(store: Store) -> None:
    with pytest.raises(InvalidArgumentsError):
        store.add_capture(b"GIF89a", source="shell", app_title=None, width=0, height=0)

    with pytest.raises(InvalidArgumentsError):
        store.add_capture(b"", source="shell", app_title=None, width=0, height=0)


def test_list_captures_can_hide_used_images(store: Store) -> None:
    used = store.add_capture(PNG, source="shell", app_title=None, width=1, height=1)
    unused = store.add_capture(JPEG, source="shell", app_title=None, width=0, height=0)

    note = store.create_node(parent_id=None, kind="note", name="用掉一张")
    store._connection.execute(
        "INSERT INTO note_images (note_id, capture_id, position) VALUES (?, ?, 0)",
        (note["id"], used["capture_id"]),
    )
    store._connection.commit()

    assert [item["capture_id"] for item in store.list_captures(limit=10, unused_only=True)] == [
        unused["capture_id"]
    ]
    assert len(store.list_captures(limit=10, unused_only=False)) == 2


def test_compose_links_images_in_order(store: Store) -> None:
    first = store.add_capture(PNG, source="shell", app_title=None, width=1, height=1)
    second = store.add_capture(JPEG, source="shell", app_title=None, width=0, height=0)

    note = store.compose(
        parent_id=None,
        capture_ids=[second["capture_id"], first["capture_id"]],
        title="第 1 讲笔记",
        body_md="## 要点",
    )

    assert note["kind"] == "note"
    assert [image["capture_id"] for image in note["images"]] == [
        second["capture_id"],
        first["capture_id"],
    ]
    assert [image["position"] for image in note["images"]] == [0, 1]
    assert store.list_captures(limit=10, unused_only=True) == []


def test_compose_rejects_unknown_capture(store: Store) -> None:
    with pytest.raises(InvalidArgumentsError):
        store.compose(parent_id=None, capture_ids=["cap_missing"], title="笔记", body_md="")


def test_compose_requires_at_least_one_image(store: Store) -> None:
    with pytest.raises(InvalidArgumentsError):
        store.compose(parent_id=None, capture_ids=[], title="笔记", body_md="")


def test_get_note_rejects_unknown_or_folder(store: Store) -> None:
    folder = store.create_node(parent_id=None, kind="folder", name="目录")

    with pytest.raises(NoteNotFoundError):
        store.get_note("nod_missing")

    with pytest.raises(NoteNotFoundError):
        store.get_note(folder["id"])


def test_data_survives_reopen(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    first = Store(root)
    capture = first.add_capture(PNG, source="shell", app_title=None, width=1, height=1)
    note = first.compose(
        parent_id=None, capture_ids=[capture["capture_id"]], title="持久化", body_md="正文"
    )
    first.close()

    second = Store(root)
    try:
        reopened = second.get_note(note["id"])
        assert reopened["name"] == "持久化"
        assert reopened["body_md"] == "正文"
        assert reopened["images"][0]["capture_id"] == capture["capture_id"]
    finally:
        second.close()


def test_database_rejects_a_folder_with_body_at_the_sql_level(store: Store) -> None:
    """CHECK 约束是最后一道闸：绕过 store API 直接写也写不进去。"""
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO nodes (id, parent_id, kind, name, position, body_md,"
            " created_at, updated_at) VALUES ('nod_x', NULL, 'folder', '坏目录', 0, '正文',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
