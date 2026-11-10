"""PhoNote 工具层：六个工具真的挂上去了，而且能通过统一入口调通。

两条路径都要验：``execute_tool``（HTTP 与 MCP 共用的那条管线）与 HTTP 的
``/v1/tools/execute``——壳走的是后者。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from driver_platform.bootstrap import create_registry_and_host
from driver_platform.config import Settings
from driver_platform.errors import InvalidArgumentsError, NoteNotFoundError
from driver_platform.main import create_app
from driver_platform.tools.executor import execute_tool

PNG_B64 = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
).decode("ascii")

EXPECTED_TOOLS = {
    "note_tree",
    "note_node_create",
    "note_add_capture",
    "note_list_captures",
    "note_compose",
    "note_get",
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(plugins=("pho_note",), notes_dir=str(tmp_path / "notes"))


@pytest.fixture
def registry(settings: Settings) -> Iterator[Any]:
    tool_registry, host = create_registry_and_host(settings)
    yield tool_registry
    host.close()


def _run(registry: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(execute_tool(registry, name, arguments)).result


def test_plugin_exposes_the_six_p0_tools(registry: Any) -> None:
    # echo 是平台自带的内置工具，不属于本插件；这里只要求六件套齐全。
    assert EXPECTED_TOOLS <= set(registry.names())
    plugin_tools = [tool for tool in registry.manifests() if tool.name.startswith("note_")]
    assert {tool.contract_version for tool in plugin_tools} == {"1.0"}


def test_capture_then_compose_then_read(registry: Any) -> None:
    capture = _run(
        registry,
        "note_add_capture",
        {"image_b64": PNG_B64, "app_title": "网课", "width": 1, "height": 1},
    )
    assert capture["media_type"] == "image/png"

    buffered = _run(registry, "note_list_captures", {"limit": 10})
    assert [item["capture_id"] for item in buffered] == [capture["capture_id"]]

    folder = _run(registry, "note_node_create", {"kind": "folder", "name": "第 1 讲"})
    note = _run(
        registry,
        "note_compose",
        {
            "parent_id": folder["id"],
            "capture_ids": [capture["capture_id"]],
            "title": "开篇",
        },
    )
    assert note["parent_id"] == folder["id"]
    assert note["images"][0]["capture_id"] == capture["capture_id"]

    read_back = _run(registry, "note_get", {"note_id": note["id"]})
    assert read_back["name"] == "开篇"
    assert len(read_back["images"]) == 1

    tree = _run(registry, "note_tree", {})
    assert [node["children"][0]["name"] for node in tree] == ["开篇"]


def test_unknown_note_is_a_dedicated_404(registry: Any) -> None:
    with pytest.raises(NoteNotFoundError) as caught:
        _run(registry, "note_get", {"note_id": "nod_missing"})

    assert caught.value.code == "note_not_found"
    assert caught.value.status_code == 404


def test_bad_base64_is_a_validation_error(registry: Any) -> None:
    with pytest.raises(InvalidArgumentsError):
        _run(registry, "note_add_capture", {"image_b64": "***不是 base64***"})


def test_http_execute_round_trip(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        listed = client.get("/v1/tools").json()
        assert EXPECTED_TOOLS <= {item["name"] for item in listed}

        created = client.post(
            "/v1/tools/execute",
            json={
                "tool": "note_add_capture",
                "arguments": {"image_b64": PNG_B64, "width": 1, "height": 1},
            },
        )
        assert created.status_code == 200
        capture_id = created.json()["result"]["capture_id"]

        composed = client.post(
            "/v1/tools/execute",
            json={
                "tool": "note_compose",
                "arguments": {"capture_ids": [capture_id], "title": "从 HTTP 来"},
            },
        )
        assert composed.status_code == 200
        note_id = composed.json()["result"]["id"]

        fetched = client.post(
            "/v1/tools/execute", json={"tool": "note_get", "arguments": {"note_id": note_id}}
        )
        assert fetched.status_code == 200
        assert fetched.json()["result"]["images"][0]["capture_id"] == capture_id

        missing = client.post(
            "/v1/tools/execute", json={"tool": "note_get", "arguments": {"note_id": "nod_missing"}}
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "note_not_found"
