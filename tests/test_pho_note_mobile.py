"""手机版（安卓）：一页 HTML + 三条无数据端点，以及它的导入路径走的是同一条 REST。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.main import create_app
from driver_platform.security.keys import parse_api_keys

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _client(tmp_path: Path, *, keys: str = "", ui: bool = True) -> TestClient:
    settings = Settings(
        plugins=("pho_note",),
        notes_dir=str(tmp_path / "notes"),
        notes_ui=ui,
        auth_mode="required" if keys else "off",
        api_keys=parse_api_keys(keys),
    )
    return TestClient(create_app(settings))


def test_mobile_page_is_served_without_credentials(tmp_path: Path) -> None:
    client = _client(tmp_path, keys="ro:notes_read|notes_write")

    # 页面本身不含数据，所以不要钥匙也能拿到；但页面里的每个请求照样要过作用域。
    response = client.get("/notes/api/m")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "PhoNote 手机版" in body
    # 页面里必须真的走 REST（api() 会拼上 /notes/api 前缀），而不是本地假数据。
    assert "api('/captures" in body
    assert "api('/notes'" in body
    assert "readAsDataURL" in body


def test_mobile_manifest_and_icon(tmp_path: Path) -> None:
    client = _client(tmp_path)

    manifest = client.get("/notes/api/m/manifest.webmanifest")
    assert manifest.status_code == 200
    payload = manifest.json()
    assert payload["start_url"] == "/notes/api/m"
    assert payload["display"] == "standalone"
    assert payload["icons"][0]["src"] == "/notes/api/m/icon.svg"

    icon = client.get("/notes/api/m/icon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert icon.text.lstrip().startswith("<svg")


def test_mobile_routes_are_absent_when_the_ui_is_off(tmp_path: Path) -> None:
    client = _client(tmp_path, ui=False)

    assert client.get("/notes/api/m").status_code == 404
    assert client.get("/notes/api/m/manifest.webmanifest").status_code == 404


def test_mobile_import_and_compose_use_the_normal_rest_path(tmp_path: Path) -> None:
    client = _client(tmp_path)

    upload = client.post(
        "/notes/api/captures",
        json={
            "image_b64": base64.b64encode(PNG).decode(),
            "source": "android",
            "app_title": "Screenshot_20261117.png",
        },
    )
    assert upload.status_code == 201, upload.text
    capture_id = upload.json()["capture_id"]

    buffer = client.get("/notes/api/captures?limit=60&unused_only=true")
    assert [item["capture_id"] for item in buffer.json()] == [capture_id]

    compose = client.post(
        "/notes/api/notes",
        json={
            "parent_id": None,
            "capture_ids": [capture_id],
            "title": "手机导入的笔记",
            "body_md": "",
        },
    )
    assert compose.status_code == 201, compose.text
    note = compose.json()
    assert note["name"] == "手机导入的笔记"

    detail: dict[str, Any] = client.get(f"/notes/api/notes/{note['id']}").json()
    assert [image["capture_id"] for image in detail["images"]] == [capture_id]
    assert client.get(f"/notes/api/captures/{capture_id}/image").status_code == 200


def test_mobile_page_loads_but_writes_need_the_key(tmp_path: Path) -> None:
    client = _client(tmp_path, keys="ro:notes_read")

    assert client.get("/notes/api/m").status_code == 200
    # 只读钥匙能读清单……
    assert client.get("/notes/api/tree", headers={"X-API-Key": "ro"}).status_code == 200
    # ……但导入图片要 notes_write，401 是「没给钥匙」，403 是「给了但不够」。
    anonymous = client.post(
        "/notes/api/captures",
        json={"image_b64": base64.b64encode(PNG).decode(), "source": "android"},
    )
    assert anonymous.status_code == 401
    denied = client.post(
        "/notes/api/captures",
        headers={"X-API-Key": "ro"},
        json={"image_b64": base64.b64encode(PNG).decode(), "source": "android"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"
