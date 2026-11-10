"""P1：树的全量增删改移、批注、版本、导出，以及给壳用的 REST 层。"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.main import create_app
from driver_platform.plugins.pho_note.store import Store
from driver_platform.security.keys import parse_api_keys

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    instance = Store(tmp_path / "notes")
    yield instance
    instance.close()


def _capture(store: Store) -> dict[str, Any]:
    return store.add_capture(PNG, source="shell", app_title=None, width=10, height=10)


# --------------------------------------------------------------------- 存储层


def test_rename_keeps_the_tree_and_rejects_sibling_conflict(store: Store) -> None:
    folder = store.create_node(parent_id=None, kind="folder", name="线性代数")
    other = store.create_node(parent_id=None, kind="folder", name="概率论")

    renamed = store.rename_node(folder["id"], "  线性代数（2026）  ")
    assert renamed["name"] == "线性代数（2026）"
    assert [node["name"] for node in store.tree()] == ["线性代数（2026）", "概率论"]

    from driver_platform.errors import NodeNameConflictError

    with pytest.raises(NodeNameConflictError) as caught:
        store.rename_node(folder["id"], other["name"])
    assert caught.value.status_code == 409
    assert caught.value.code == "node_name_conflict"


def test_move_into_own_subtree_is_rejected(store: Store) -> None:
    from driver_platform.errors import NodeMoveInvalidError

    outer = store.create_node(parent_id=None, kind="folder", name="外层")
    inner = store.create_node(parent_id=outer["id"], kind="folder", name="内层")

    with pytest.raises(NodeMoveInvalidError):
        store.move_node(outer["id"], parent_id=inner["id"])
    with pytest.raises(NodeMoveInvalidError):
        store.move_node(outer["id"], parent_id=outer["id"])

    note = store.create_node(parent_id=None, kind="note", name="笔记")
    with pytest.raises(NodeMoveInvalidError):
        store.move_node(outer["id"], parent_id=note["id"])

    moved = store.move_node(inner["id"], parent_id=None)
    assert moved["parent_id"] is None
    # 移到根后排在最后（position 取同层最大值 +1），不是插到最前面。
    assert [node["name"] for node in store.tree()] == ["外层", "笔记", "内层"]


def test_delete_requires_recursive_and_keeps_the_images(store: Store) -> None:
    from driver_platform.errors import InvalidArgumentsError

    course = store.create_node(parent_id=None, kind="folder", name="课程")
    note = store.create_node(parent_id=course["id"], kind="note", name="第 1 讲")
    capture = _capture(store)
    store.set_images(note["id"], [capture["capture_id"]])

    with pytest.raises(InvalidArgumentsError):
        store.delete_node(course["id"], recursive=False)

    outcome = store.delete_node(course["id"], recursive=True)
    assert outcome == {"deleted": 2, "node_id": course["id"]}
    assert store.tree() == []
    assert store.list_captures(limit=10, unused_only=False)


def test_editing_a_note_keeps_a_revision_and_rollback_works(store: Store) -> None:
    note = store.create_node(parent_id=None, kind="note", name="矩阵", body_md="第一版")
    store.update_note(note["id"], body_md="第二版", reason="ai-summary")

    revisions = store.list_revisions(note["id"])
    assert [item["body_md"] for item in revisions] == ["第一版"]
    assert revisions[0]["reason"] == "ai-summary"

    rolled = store.rollback(note["id"], revisions[0]["revision_id"])
    assert rolled["body_md"] == "第一版"
    # 回滚本身也留一个版本：再撤回一次就能回到第二版。
    assert [item["body_md"] for item in store.list_revisions(note["id"])] == [
        "第二版",
        "第一版",
    ]


def test_search_matches_name_and_body_and_escapes_wildcards(store: Store) -> None:
    store.create_node(parent_id=None, kind="note", name="矩阵乘法", body_md="")
    store.create_node(parent_id=None, kind="note", name="概率", body_md="全概率公式 100%")
    store.create_node(parent_id=None, kind="folder", name="矩阵目录")

    assert [item["name"] for item in store.list_notes(query="矩阵")] == ["矩阵乘法"]
    assert [item["name"] for item in store.list_notes(query="100%")] == ["概率"]
    # 百分号当普通字符：没有任何一条正文含字面量 "0%" 以外的匹配项。
    assert store.list_notes(query="%") != store.list_notes(query="")
    assert [item["name"] for item in store.list_notes(query="_")] == []


def test_set_images_reorders_and_removes(store: Store) -> None:
    note = store.create_node(parent_id=None, kind="note", name="笔记")
    first, second = (
        _capture(store),
        store.add_capture(PNG + b"2", source="shell", app_title=None, width=1, height=1),
    )

    store.set_images(note["id"], [first["capture_id"], second["capture_id"]])
    reordered = store.set_images(note["id"], [second["capture_id"]])
    assert [image["capture_id"] for image in reordered["images"]] == [second["capture_id"]]

    emptied = store.set_images(note["id"], [])
    assert emptied["images"] == []


def test_annotations_round_trip_and_validate(store: Store) -> None:
    from driver_platform.errors import InvalidArgumentsError

    note = store.create_node(parent_id=None, kind="note", name="笔记", body_md="正文")
    created = store.add_annotation(
        note["id"],
        kind="text",
        anchor={"quote": "正文", "prefix": "", "suffix": "", "offset": 0},
        body_md="这里要补一个例子",
        color="green",
    )
    assert created["kind"] == "text"
    assert created["anchor"]["offset"] == 0

    fetched = store.get_note(note["id"])
    assert [item["annotation_id"] for item in fetched["annotations"]] == [created["annotation_id"]]

    with pytest.raises(InvalidArgumentsError):
        store.add_annotation(note["id"], kind="sticker", anchor={}, body_md="x")
    with pytest.raises(InvalidArgumentsError):
        store.add_annotation(note["id"], kind="note", anchor={}, body_md="   ")

    store.delete_annotation(created["annotation_id"])
    assert store.get_note(note["id"])["annotations"] == []


def test_export_uses_absolute_image_paths(store: Store) -> None:
    note = store.create_node(parent_id=None, kind="note", name="标题", body_md="正文一段")
    capture = _capture(store)
    store.set_images(note["id"], [capture["capture_id"]])

    exported = store.export_markdown(note["id"])
    assert exported["markdown"].startswith("# 标题\n\n正文一段")
    assert exported["images"][0]["path"].endswith(".png")
    assert exported["images"][0]["path"] in exported["markdown"]


# --------------------------------------------------------------------- REST 层


def _client(tmp_path: Path, *, keys: str = "", ui: bool = True) -> TestClient:
    settings = Settings(
        plugins=("pho_note",),
        notes_dir=str(tmp_path / "notes"),
        notes_ui=ui,
        auth_mode="required" if keys else "off",
        api_keys=parse_api_keys(keys),
    )
    return TestClient(create_app(settings))


def _upload(client: TestClient, headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = client.post(
        "/notes/api/captures",
        json={"image_b64": base64.b64encode(PNG).decode(), "source": "shell"},
        headers=headers or {},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_rest_tree_compose_and_read_back(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        folder = client.post("/notes/api/nodes", json={"kind": "folder", "name": "课程"}).json()
        capture = _upload(client)
        composed = client.post(
            "/notes/api/notes",
            json={
                "parent_id": folder["id"],
                "capture_ids": [capture["capture_id"]],
                "title": "第 1 讲",
                "body_md": "正文",
            },
        )
        assert composed.status_code == 201, composed.text
        note_id = composed.json()["id"]

        tree = client.get("/notes/api/tree").json()
        assert [node["name"] for node in tree] == ["课程"]
        assert [child["name"] for child in tree[0]["children"]] == ["第 1 讲"]

        note = client.get(f"/notes/api/notes/{note_id}").json()
        assert note["body_md"] == "正文"
        assert [image["capture_id"] for image in note["images"]] == [capture["capture_id"]]

        patched = client.patch(f"/notes/api/notes/{note_id}", json={"body_md": "改过"})
        assert patched.json()["body_md"] == "改过"
        revisions = client.get(f"/notes/api/notes/{note_id}/revisions").json()
        assert [item["body_md"] for item in revisions] == ["正文"]

        exported = client.get(f"/notes/api/notes/{note_id}/export")
        assert exported.status_code == 200
        assert exported.text.startswith("# 第 1 讲")

        image = client.get(f"/notes/api/captures/{capture['capture_id']}/image")
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/png")
        assert image.content == PNG

        assert client.delete(f"/notes/api/nodes/{folder['id']}").status_code == 422


def test_rest_read_only_key_cannot_write(tmp_path: Path) -> None:
    keys = "ro:notes_read,rw:notes_read|notes_write"
    with _client(tmp_path, keys=keys) as client:
        reader = {"X-API-Key": "ro"}
        writer = {"X-API-Key": "rw"}

        assert client.get("/notes/api/tree", headers=reader).status_code == 200
        denied = client.post(
            "/notes/api/nodes", json={"kind": "folder", "name": "x"}, headers=reader
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "permission_denied"

        assert (
            client.post(
                "/notes/api/nodes", json={"kind": "folder", "name": "x"}, headers=writer
            ).status_code
            == 201
        )
        assert client.get("/notes/api/tree").status_code == 401


def test_rest_is_absent_when_the_ui_flag_is_off(tmp_path: Path) -> None:
    with _client(tmp_path, ui=False) as client:
        assert client.get("/notes/api/tree").status_code == 404
