"""P3：备份 zip、WebDAV 推送/拉取、单主设备的「未推送改动」判据。

WebDAV 用 ``httpx.MockTransport`` 起一个内存里的假服务器（MKCOL/PUT/GET/DELETE），
所以这些用例既跑真逻辑，又完全不碰网络。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.errors import SyncFailedError, SyncNotConfiguredError
from driver_platform.main import create_app
from driver_platform.plugins.pho_note import sync as sync_module
from driver_platform.plugins.pho_note.ai import AIConfig, NoteAI
from driver_platform.plugins.pho_note.store import Store
from driver_platform.plugins.pho_note.sync import SyncConfig, pull, push, status
from driver_platform.plugins.pho_note.tools import build_tools
from driver_platform.security.keys import parse_api_keys
from driver_platform.tools.executor import execute_tool
from driver_platform.tools.registry import ToolRegistry

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"
CONFIG = SyncConfig(url="http://dav.test/", keep=2)


def fake_dav(remote: dict[str, bytes]) -> httpx.MockTransport:
    """够用的假 WebDAV：路径去前导斜杠做 key。"""

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        if request.method == "MKCOL":
            return httpx.Response(405 if key in remote else 201)
        if request.method == "PUT":
            remote[key] = request.content
            return httpx.Response(201)
        if request.method == "GET":
            if key not in remote:
                return httpx.Response(404)
            return httpx.Response(200, content=remote[key])
        if request.method == "DELETE":
            remote.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)

    return httpx.MockTransport(handler)


def seed(store: Store) -> dict[str, Any]:
    """一棵目录 + 一篇带图笔记，够验证「库 + 媒体」两条同步线。"""

    folder = store.create_node(parent_id=None, kind="folder", name="课程")
    capture = store.add_capture(PNG, source="test", app_title="网课", width=8, height=8)
    store.add_capture(JPEG, source="test", app_title="网课", width=8, height=8)
    note = store.compose(
        parent_id=folder["id"],
        capture_ids=[capture["capture_id"]],
        title="第一讲",
        body_md="触发器与视图",
    )
    return {"folder": folder, "note": note, "capture": capture}


# ---------------------------------------------------------------- 备份


def test_backup_zip_contains_the_database_and_media(tmp_path: Any) -> None:
    store = Store(tmp_path)
    try:
        seed(store)
        result = store.backup_zip()
        path = result["path"]
        assert result["media_files"] == 2
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            assert "phonote.db" in names
            media = [name for name in names if name.startswith("media/")]
            assert len(media) == 2
            # zip 里的库能独立打开，且数据在里面（不是空壳）。
            restored = tmp_path / "restored.db"
            restored.write_bytes(archive.read("phonote.db"))
            with sqlite3.connect(restored) as connection:
                assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2
    finally:
        store.close()


def test_snapshot_is_a_consistent_copy(tmp_path: Any) -> None:
    store = Store(tmp_path)
    try:
        seed(store)
        blob = store.snapshot_bytes()
        assert blob.startswith(b"SQLite format 3")
        snapshot = tmp_path / "copy.db"
        snapshot.write_bytes(blob)
        with sqlite3.connect(snapshot) as connection:
            assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 2
    finally:
        store.close()


# ---------------------------------------------------------------- 推送 / 拉取


def test_push_uploads_media_snapshot_and_manifest(tmp_path: Any) -> None:
    remote: dict[str, bytes] = {}
    store = Store(tmp_path / "one")
    try:
        seed(store)
        summary = push(store, CONFIG, transport=fake_dav(remote))
        assert summary["uploaded_media"] == 2
        assert summary["media_total"] == 2

        manifest = json.loads(remote["phonote/manifest.json"])
        assert manifest["version"] == 1
        assert manifest["db"].startswith("db/")
        assert remote[f"phonote/{manifest['db']}"].startswith(b"SQLite format 3")
        assert len(manifest["media"]) == 2
        assert manifest["db_sha256"] == summary["db_sha256"]
        # 本地 sync.json 记下推送点，pull 的脏检查靠它。
        state = json.loads((store.root / "sync.json").read_text(encoding="utf-8"))
        assert state["pushed_db_sha256"] == summary["db_sha256"]
    finally:
        store.close()


def test_second_push_only_uploads_missing_media(tmp_path: Any) -> None:
    remote: dict[str, bytes] = {}
    store = Store(tmp_path / "one")
    try:
        seed(store)
        push(store, CONFIG, transport=fake_dav(remote))
        store.create_node(parent_id=None, kind="folder", name="第二讲")
        summary = push(store, CONFIG, transport=fake_dav(remote))
        assert summary["uploaded_media"] == 0
        assert summary["media_total"] == 2
    finally:
        store.close()


def test_push_prunes_old_snapshots_beyond_keep(tmp_path: Any) -> None:
    remote: dict[str, bytes] = {}
    store = Store(tmp_path / "one")
    try:
        seed(store)
        push(store, CONFIG, transport=fake_dav(remote))  # keep=2
        store.create_node(parent_id=None, kind="folder", name="第二次")
        push(store, CONFIG, transport=fake_dav(remote))
        store.create_node(parent_id=None, kind="folder", name="第三次")
        summary = push(store, CONFIG, transport=fake_dav(remote))

        assert len(summary["pruned"]) == 1
        manifest = json.loads(remote["phonote/manifest.json"])
        assert len(manifest["snapshots"]) == 2
        assert summary["pruned"][0] not in manifest["snapshots"]
        assert f"phonote/{summary['pruned'][0]}" not in remote
    finally:
        store.close()


def test_pull_restores_notes_and_media_on_a_second_device(tmp_path: Any) -> None:
    remote: dict[str, bytes] = {}
    transport = fake_dav(remote)
    source = Store(tmp_path / "one")
    try:
        seed(source)
        push(source, CONFIG, transport=transport)
    finally:
        source.close()

    target = Store(tmp_path / "two")
    try:
        result = pull(target, CONFIG, transport=transport)
        assert result["fetched_media"] == 2
        assert result["backup"].endswith(".db")

        tree = target.tree()
        assert [node["name"] for node in tree] == ["课程"]
        note = target.tree(include_body=True)[0]["children"][0]
        assert note["name"] == "第一讲"
        assert note["body_md"] == "触发器与视图"
        assert len(target.media_files()) == 2
        # 快照里的 sha 与本地落盘文件一致（不是写了个空文件）。
        for item in target.media_files():
            assert (target.root / item["path"]).is_file()
    finally:
        target.close()


def test_pull_refuses_when_local_has_unpushed_changes(tmp_path: Any) -> None:
    remote: dict[str, bytes] = {}
    transport = fake_dav(remote)
    store = Store(tmp_path / "one")
    try:
        seed(store)
        push(store, CONFIG, transport=transport)
        pull(store, CONFIG, transport=transport)  # 拉一次，状态对齐
        store.create_node(parent_id=None, kind="folder", name="本地新改动")

        with pytest.raises(SyncFailedError) as caught:
            pull(store, CONFIG, transport=transport)
        assert "未推送" in str(caught.value)
    finally:
        store.close()


def test_pull_detects_a_corrupted_snapshot(tmp_path: Any) -> None:
    remote: dict[str, bytes] = {}
    transport = fake_dav(remote)
    source = Store(tmp_path / "one")
    try:
        seed(source)
        push(source, CONFIG, transport=transport)
    finally:
        source.close()

    manifest = json.loads(remote["phonote/manifest.json"])
    remote[f"phonote/{manifest['db']}"] = b"SQLite format 3" + b"tampered"

    target = Store(tmp_path / "two")
    try:
        with pytest.raises(SyncFailedError) as caught:
            pull(target, CONFIG, transport=transport)
        assert "校验失败" in str(caught.value)
    finally:
        target.close()


def test_push_without_a_remote_is_rejected(tmp_path: Any) -> None:
    store = Store(tmp_path)
    try:
        with pytest.raises(SyncNotConfiguredError) as caught:
            push(store, SyncConfig())
        assert "DRIVER_NOTES_WEBDAV_URL" in str(caught.value)
    finally:
        store.close()


def test_status_reports_configuration_and_dirty_state(tmp_path: Any) -> None:
    remote: dict[str, bytes] = {}
    transport = fake_dav(remote)
    store = Store(tmp_path)
    try:
        seed(store)
        fresh = status(store, CONFIG)
        assert fresh["configured"] is True
        assert fresh["dirty"] is False

        push(store, CONFIG, transport=transport)
        pushed = status(store, CONFIG)
        assert pushed["dirty"] is False
        assert pushed["snapshot"].startswith("db/")

        store.create_node(parent_id=None, kind="folder", name="又改了一笔")
        assert status(store, CONFIG)["dirty"] is True
        assert status(store, SyncConfig())["configured"] is False
    finally:
        store.close()


# ---------------------------------------------------------------- 工具层


def test_sync_tools_report_a_searchable_error_code(tmp_path: Any) -> None:
    store = Store(tmp_path)
    try:
        registry = ToolRegistry()
        for tool in build_tools(store, NoteAI(AIConfig()), SyncConfig()):
            registry.register(tool)
        assert {"note_backup", "note_sync_push", "note_sync_pull", "note_sync_status"} <= set(
            registry.names()
        )
        with pytest.raises(SyncNotConfiguredError) as caught:
            asyncio.run(execute_tool(registry, "note_sync_push", {}))
        assert caught.value.code == "sync_not_configured"
        backed = asyncio.run(execute_tool(registry, "note_backup", {}))
        assert backed.result["bytes"] > 0
    finally:
        store.close()


# ---------------------------------------------------------------- REST 层


def test_rest_sync_endpoints_use_the_plugin_configuration(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote: dict[str, bytes] = {}
    transport = fake_dav(remote)

    class Injected(sync_module.WebDAVClient):
        def __init__(self, config: SyncConfig, **kwargs: Any) -> None:
            super().__init__(config, transport=transport)

    monkeypatch.setattr(sync_module, "WebDAVClient", Injected)
    settings = Settings(
        plugins=("pho_note",),
        notes_dir=str(tmp_path),
        notes_ui=True,
        notes_webdav_url="http://dav.test/",
        auth_mode="required",
        api_keys=parse_api_keys("rw:notes_read|notes_write,ro:notes_read"),
    )
    with TestClient(create_app(settings)) as client:
        headers = {"X-API-Key": "rw"}
        state = client.get("/notes/api/sync/status", headers=headers)
        assert state.status_code == 200
        assert state.json()["configured"] is True

        pushed = client.post("/notes/api/sync/push", headers=headers)
        assert pushed.status_code == 200
        assert pushed.json()["pushed"] is True

        pulled = client.post("/notes/api/sync/pull", headers=headers)
        assert pulled.status_code == 200
        assert pulled.json()["pulled"] is True

        backup = client.post("/notes/api/backup", headers=headers)
        assert backup.status_code == 200
        assert backup.json()["bytes"] > 0

        # 只读钥匙能看状态，不能推送。
        readonly = client.post("/notes/api/sync/push", headers={"X-API-Key": "ro"})
        assert readonly.status_code == 403
        assert readonly.json()["error"]["code"] == "permission_denied"
