"""PhoNote P2：AI 总结 / 讨论 / 增量补丁。

模型的 HTTP 层用 ``httpx.MockTransport`` 顶掉，全程不联网；这里要钉住的是
「请求长得对不对」「解析与套用补丁的边界」「off 时明确报错而不是静默什么都不做」。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from driver_platform.config import Settings
from driver_platform.errors import (
    AiNotConfiguredError,
    AiReplyUnparsableError,
    NoteNotFoundError,
    PermissionDeniedError,
)
from driver_platform.main import create_app
from driver_platform.plugins.pho_note.ai import (
    AIConfig,
    NoteAI,
    OpenAICompatProvider,
    apply_edits,
    build_provider,
    parse_reply,
)
from driver_platform.plugins.pho_note.store import Store
from driver_platform.security.keys import Principal, parse_api_keys

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _model_reply(summary: str = "要点") -> httpx.Response:
    payload = {"summary": summary, "edits": [{"find": "原始", "replace": "改后"}]}
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})


def _provider(handler: Any, *, model: str = "test-model") -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url="https://model.test/v1",
        api_key="sk-test",
        model=model,
        transport=httpx.MockTransport(handler),
    )


def _note(store: Store, *, body: str = "原始正文") -> str:
    capture = store.add_capture(PNG, source="test", app_title="板书", width=1600, height=900)
    composed = store.compose(
        parent_id=None,
        capture_ids=[capture["capture_id"]],
        title="笔记",
        body_md=body,
    )
    return str(composed["id"])


# --------------------------------------------------------------------- 解析与套用


def test_parse_reply_strips_a_json_fence() -> None:
    fenced = '```json\n{"summary": "小标题", "edits": [{"find": "a", "replace": "b"}]}\n```'
    parsed = parse_reply(fenced)
    assert parsed["summary"] == "小标题"
    assert parsed["edits"] == [{"find": "a", "replace": "b"}]


def test_parse_reply_rejects_non_json_and_bad_edits() -> None:
    with pytest.raises(AiReplyUnparsableError):
        parse_reply("这不是 JSON")
    with pytest.raises(AiReplyUnparsableError):
        parse_reply('{"edits": "不是数组"}')
    with pytest.raises(AiReplyUnparsableError):
        parse_reply('{"edits": [{"find": 3}]}')


def test_apply_edits_only_touches_unique_anchors() -> None:
    body = "甲\n乙\n丙"
    # 0 号唯一命中 → 替换；1 号出现两次 → 跳过；2 号不存在 → 跳过；3 号空 find → 追加。
    edited, applied, skipped = apply_edits(
        body,
        [
            {"find": "乙", "replace": "乙（改）"},
            {"find": "不存在甲", "replace": "x"},
            {"find": "不存在", "replace": "y"},
            {"find": "", "replace": "结尾"},
        ],
    )
    assert edited == "甲\n乙（改）\n丙\n结尾"
    assert applied == [0, 3]
    assert skipped == [1, 2]


def test_apply_edits_with_duplicate_anchor_changes_nothing() -> None:
    edited, applied, skipped = apply_edits("重复 重复", [{"find": "重复", "replace": "一次"}])
    assert edited == "重复 重复"
    assert applied == []
    assert skipped == [0]


# --------------------------------------------------------------------- 门面


def test_off_mode_refuses_loudly() -> None:
    ai = NoteAI(AIConfig(mode="off"))
    with pytest.raises(AiNotConfiguredError) as caught:
        asyncio.run(
            ai.act(note={"id": "n", "name": "n", "body_md": ""}, annotations=[], instruction="x")
        )
    assert "DRIVER_NOTES_LLM" in str(caught.value)


def test_build_provider_requires_credentials() -> None:
    assert build_provider(AIConfig(mode="off")) is None
    with pytest.raises(AiNotConfiguredError):
        build_provider(AIConfig(mode="openai"))


def test_manual_mode_returns_a_bundle_without_calling_the_model() -> None:
    ai = NoteAI(AIConfig(mode="manual"))
    note = {"id": "nod_1", "name": "笔记", "body_md": "正文", "images": []}
    bundle = ai.manual_bundle(note=note, annotations=[], instruction="总结一下")
    assert bundle["mode"] == "manual"
    assert "总结一下" in bundle["copy_text"]
    assert bundle["note_id"] == "nod_1"


def test_openai_request_carries_json_mode_and_images() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return _model_reply()

    ai = NoteAI(
        AIConfig(mode="openai", base_url="https://model.test/v1", api_key="sk-test"),
        provider=_provider(handler),
    )
    note = {"id": "nod_1", "name": "笔记", "body_md": "正文", "images": []}
    outcome = asyncio.run(
        ai.act(note=note, annotations=[], instruction="总结", images=[("image/png", PNG)])
    )

    assert outcome["summary"] == "要点"
    assert outcome["model"] == "test-model"
    assert seen["url"] == "https://model.test/v1/chat/completions"
    payload = seen["payload"]
    assert payload["response_format"] == {"type": "json_object"}
    image_parts = [
        part for part in payload["messages"][-1]["content"] if part["type"] == "image_url"
    ]
    assert image_parts
    assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_transport_failure_is_reported_as_unavailable() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    ai = NoteAI(
        AIConfig(mode="openai", base_url="https://model.test/v1", api_key="sk-test"),
        provider=_provider(boom),
    )
    note = {"id": "n", "name": "n", "body_md": ""}
    with pytest.raises(Exception) as caught:
        asyncio.run(ai.act(note=note, annotations=[], instruction="x"))
    assert getattr(caught.value, "code", "") == "ai_unavailable"
    assert "ConnectError" in str(caught.value)


def test_missing_choices_becomes_unavailable() -> None:
    def empty(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x"})

    ai = NoteAI(
        AIConfig(mode="openai", base_url="https://model.test/v1", api_key="sk-test"),
        provider=_provider(empty),
    )
    note = {"id": "n", "name": "n", "body_md": ""}
    with pytest.raises(Exception) as caught:
        asyncio.run(ai.act(note=note, annotations=[], instruction="x"))
    assert getattr(caught.value, "code", "") == "ai_unavailable"
    assert "choices" in str(caught.value)


def test_build_provider_defaults_by_mode() -> None:
    from driver_platform.plugins.pho_note.ai import AnthropicProvider

    anthropic = build_provider(AIConfig(mode="anthropic", api_key="k"))
    assert isinstance(anthropic, AnthropicProvider)
    assert anthropic.model == "claude-sonnet-4-5"
    openai = build_provider(AIConfig(mode="openai", api_key="k"))
    assert isinstance(openai, OpenAICompatProvider)
    assert openai.model == "gpt-4o-mini"


def test_http_failure_becomes_ai_unavailable_without_echoing_the_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key sk-secret"})

    ai = NoteAI(
        AIConfig(mode="openai", base_url="https://model.test/v1", api_key="sk-test"),
        provider=_provider(handler),
    )
    note = {"id": "n", "name": "n", "body_md": ""}
    with pytest.raises(Exception) as caught:
        asyncio.run(ai.act(note=note, annotations=[], instruction="x"))
    assert getattr(caught.value, "code", "") == "ai_unavailable"
    assert "sk-secret" not in str(caught.value)


# --------------------------------------------------------------------- 处理器


def test_chat_records_both_turns_and_feeds_history(tmp_path: Path) -> None:
    from driver_platform.plugins.pho_note.tools import run_chat

    store = Store(tmp_path / "notes")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["messages"] = json.loads(request.content)["messages"]
        return _model_reply("已按你的要求整理")

    ai = NoteAI(
        AIConfig(mode="openai", base_url="https://model.test/v1", api_key="sk-test"),
        provider=_provider(handler),
    )
    try:
        note_id = _note(store)
        outcome = asyncio.run(run_chat(store, ai, note_id, "把第二行删掉"))
        assert outcome["summary"] == "已按你的要求整理"
        assert [item["role"] for item in store.list_messages(note_id)] == [
            "user",
            "assistant",
        ]
        roles = [item["role"] for item in seen["messages"]]
        assert roles[0] == "system"
        assert "把第二行删掉" in json.dumps(seen["messages"], ensure_ascii=False)

        asyncio.run(run_chat(store, ai, note_id, "再补一句"))
        # 第二次提问把上一轮带进历史（system + 历史 + 新问题）。
        assert len(seen["messages"]) >= 4
    finally:
        store.close()


def test_summarize_records_the_exchange_and_sends_the_image(tmp_path: Path) -> None:
    from driver_platform.plugins.pho_note.tools import run_summarize

    store = Store(tmp_path / "notes")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return _model_reply("板书要点")

    ai = NoteAI(
        AIConfig(mode="openai", base_url="https://model.test/v1", api_key="sk-test", max_images=1),
        provider=_provider(handler),
    )
    try:
        note_id = _note(store)
        outcome = asyncio.run(run_summarize(store, ai, note_id, "整理要点"))
        assert outcome["summary"] == "板书要点"
        assert [item["role"] for item in store.list_messages(note_id)] == [
            "user",
            "assistant",
        ]
        parts = seen["payload"]["messages"][-1]["content"]
        assert any(part["type"] == "image_url" for part in parts)
    finally:
        store.close()


def test_anthropic_request_shape_and_reply_blocks() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]
            },
        )

    from driver_platform.plugins.pho_note.ai import AnthropicProvider

    provider = AnthropicProvider(
        base_url="https://anthropic.test",
        api_key="sk-ant",
        model="claude-test",
        transport=httpx.MockTransport(handler),
    )
    text = asyncio.run(
        provider.complete(
            [{"role": "system", "content": "只输出 JSON"}, {"role": "user", "content": "看图"}],
            images=[("image/png", PNG)],
            json_mode=True,
        )
    )
    assert text == "第一段第二段"
    assert seen["url"] == "https://anthropic.test/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    payload = seen["payload"]
    assert payload["system"] == "只输出 JSON"
    assert payload["max_tokens"] == 2048
    assert payload["messages"][0]["content"][1]["source"]["type"] == "base64"


def test_non_json_and_bad_shapes_become_ai_unavailable() -> None:
    from driver_platform.plugins.pho_note.ai import AnthropicProvider

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    ai = NoteAI(
        AIConfig(mode="openai", base_url="https://model.test/v1", api_key="sk-test"),
        provider=_provider(not_json),
    )
    note = {"id": "n", "name": "n", "body_md": ""}
    with pytest.raises(Exception) as caught:
        asyncio.run(ai.act(note=note, annotations=[], instruction="x"))
    assert getattr(caught.value, "code", "") == "ai_unavailable"

    def bad_blocks(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": []})

    provider = AnthropicProvider(
        base_url="https://anthropic.test",
        api_key="k",
        model="m",
        transport=httpx.MockTransport(bad_blocks),
    )
    with pytest.raises(Exception) as caught:
        asyncio.run(provider.complete([{"role": "user", "content": "x"}]))
    assert getattr(caught.value, "code", "") == "ai_unavailable"


# --------------------------------------------------------------------- 存储：消息


def test_messages_round_trip_and_unknown_note(tmp_path: Path) -> None:
    store = Store(tmp_path / "notes")
    try:
        note_id = _note(store)
        first = store.add_message(note_id, role="user", body="问")
        store.add_message(note_id, role="assistant", body="答")
        messages = store.list_messages(note_id)
        assert [item["role"] for item in messages] == ["user", "assistant"]
        assert messages[0]["message_id"] == first["message_id"]
        assert store.list_messages(note_id, limit=1) == messages[:1]
        with pytest.raises(NoteNotFoundError):
            store.list_messages("nod_missing")
    finally:
        store.close()


# --------------------------------------------------------------------- REST


def _client(tmp_path: Path, *, keys: str = "", llm: str = "off") -> TestClient:
    settings = Settings(
        plugins=("pho_note",),
        notes_dir=str(tmp_path / "notes"),
        notes_ui=True,
        notes_llm=llm,
        auth_mode="required" if keys else "off",
        api_keys=parse_api_keys(keys) if keys else {},
    )
    return TestClient(create_app(settings))


def _node(client: TestClient, name: str, *, key: str | None = None) -> str:
    headers = {"X-API-Key": key} if key else {}
    created = client.post("/notes/api/nodes", json={"kind": "note", "name": name}, headers=headers)
    return str(created.json()["id"])


def test_tool_surface_has_the_five_ai_tools(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        names = {tool["name"] for tool in client.get("/v1/tools").json()}
    expected = {
        "note_summarize",
        "note_chat",
        "note_messages",
        "note_apply_patch",
        "note_ingest_ai_reply",
    }
    assert expected <= names


def test_summarize_in_manual_mode_returns_a_bundle(tmp_path: Path) -> None:
    with _client(tmp_path, llm="manual") as client:
        note_id = _node(client, "手记")
        response = client.post(
            f"/notes/api/notes/{note_id}/summarize", json={"instruction": "总结"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "manual"
        assert "总结" in body["copy_text"]
        # manual 不调模型，所以讨论区不会凭空多出消息。
        assert client.get(f"/notes/api/notes/{note_id}/messages").json() == []


def test_apply_endpoint_rewrites_the_body_and_keeps_a_revision(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        note_id = _node(client, "旧")
        client.patch(f"/notes/api/notes/{note_id}", json={"body_md": "原始正文"})

        applied = client.post(
            f"/notes/api/notes/{note_id}/apply", json={"find": "原始", "replace": "改后"}
        )
        assert applied.status_code == 200
        assert applied.json()["changed"] is True

        note = client.get(f"/notes/api/notes/{note_id}").json()
        assert note["body_md"] == "改后正文"
        revisions = client.get(f"/notes/api/notes/{note_id}/revisions").json()
        assert any(item["reason"] == "ai-patch" for item in revisions)


def test_apply_endpoint_skips_a_non_unique_anchor(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        note_id = _node(client, "重复")
        client.patch(f"/notes/api/notes/{note_id}", json={"body_md": "同 同"})
        applied = client.post(
            f"/notes/api/notes/{note_id}/apply", json={"find": "同", "replace": "异"}
        ).json()
        assert applied["changed"] is False
        assert applied["skipped"] == [0]
        assert client.get(f"/notes/api/notes/{note_id}").json()["body_md"] == "同 同"


def test_ai_endpoints_need_the_write_scope(tmp_path: Path) -> None:
    keys = "ro:notes_read,rw:notes_read|notes_write"
    with _client(tmp_path, keys=keys) as client:
        note_id = _node(client, "x", key="rw")
        denied = client.post(
            f"/notes/api/notes/{note_id}/apply",
            json={"find": "", "replace": "x"},
            headers={"X-API-Key": "ro"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "permission_denied"
        allowed = client.post(
            f"/notes/api/notes/{note_id}/apply",
            json={"find": "", "replace": "追加"},
            headers={"X-API-Key": "rw"},
        )
        assert allowed.status_code == 200


def test_ingest_tool_applies_a_pasted_reply(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        note_id = _node(client, "粘贴")
        client.patch(f"/notes/api/notes/{note_id}", json={"body_md": "原始正文"})
        reply = json.dumps(
            {"summary": "改了第一行", "edits": [{"find": "原始", "replace": "改后"}]}
        )
        response = client.post(
            "/v1/tools/execute",
            json={
                "tool": "note_ingest_ai_reply",
                "arguments": {"note_id": note_id, "reply": reply},
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["changed"] is True
        assert result["body_md"] == "改后正文"
        messages = client.get(f"/notes/api/notes/{note_id}/messages").json()
        assert [item["role"] for item in messages] == ["assistant"]
        assert messages[0]["body"] == "改了第一行"


def test_scope_guard_lets_full_power_through() -> None:
    from driver_platform.plugins.pho_note.api import _guard

    _guard(Principal(fingerprint="all", scopes=None), "notes_write")
    with pytest.raises(PermissionDeniedError):
        _guard(Principal(fingerprint="ro", scopes=frozenset({"notes_read"})), "notes_write")
