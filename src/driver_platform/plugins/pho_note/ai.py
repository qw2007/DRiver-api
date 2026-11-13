"""AI 接入：一个 ``LLMProvider`` 协议、三种实现、以及「增量补丁」的解析与落盘。

为什么不引第三方 SDK：两个 HTTP 端点、一份 JSON 补丁格式，自己写比引 ``openai`` +
``anthropic`` 两个 SDK 再加一层适配更短；而且远程宿主（阶段 6）已经是这么做的。
没有 Pillow 就不缩图：图片按原字节发送，用张数上限控制成本。
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from driver_platform.errors import (
    AiNotConfiguredError,
    AiReplyUnparsableError,
    AiUnavailableError,
)
from driver_platform.observability.context import outbound_trace_headers

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_IMAGES = 6
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 2048

MODES = ("off", "openai", "anthropic", "manual")
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-4-5"}

SYSTEM_PROMPT = (
    "你是 PhoNote 的笔记助手。任务是把网课截图与已有笔记整理成结构化 Markdown。\n"
    "只输出一个 JSON 对象，不要输出别的文字，格式：\n"
    '{"summary": "<要点或修订说明>", "edits": [{"find": "<正文里逐字出现的原文>", '
    '"replace": "<替换成的文字>"}]}\n'
    "规则：find 必须是正文里**逐字出现且只出现一次**的片段（可以是空串，表示追加）；"
    "不要编造截图里没有的内容；不确定的地方用「待确认」标注；没有要改的地方 edits 给空数组。"
)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMProvider(Protocol):
    @property
    def model(self) -> str: ...

    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        images: Sequence[tuple[str, bytes]] = (),
        json_mode: bool = False,
    ) -> str: ...


@dataclass(frozen=True)
class AIConfig:
    """从环境变量解析出来的 AI 配置；``off`` 时平台完全不会发外呼。"""

    mode: str = "off"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_images: int = DEFAULT_MAX_IMAGES

    @property
    def enabled(self) -> bool:
        return self.mode in {"openai", "anthropic"}

    def resolved_base_url(self) -> str:
        return (self.base_url or DEFAULT_BASE_URLS.get(self.mode, "")).rstrip("/")

    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.mode, "")


def _data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def _post(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """POST 一次并解成 JSON；传输层与 HTTP 层的失败都翻成 ``ai_unavailable``。

    ``transport`` 只给测试注入（``httpx.MockTransport``），生产走默认传输。
    """
    try:
        with httpx.Client(transport=transport, timeout=DEFAULT_TIMEOUT) as client:
            # traceparent 一并发给模型服务：它若回显在响应头里，日志能把两边对上。
            response = client.post(
                url, headers={**headers, **outbound_trace_headers()}, json=payload
            )
    except httpx.HTTPError as exc:
        raise AiUnavailableError(f"模型服务连不上（{type(exc).__name__}）") from exc
    if response.status_code >= 400:
        # 只回状态码，不回响应体：里面可能含请求原文与 key 指纹。
        raise AiUnavailableError(f"模型服务返回 HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise AiUnavailableError("模型服务返回的不是 JSON") from exc


class OpenAICompatProvider:
    """OpenAI 的 ``/chat/completions``，也是绝大多数自建网关的兼容形状。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        images: Sequence[tuple[str, bytes]] = (),
        json_mode: bool = False,
    ) -> str:
        converted: list[dict[str, Any]] = []
        for message in messages:
            text = str(message.get("content", ""))
            if message.get("role") == "user" and images and message is messages[-1]:
                parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
                parts.extend(
                    {"type": "image_url", "image_url": {"url": _data_url(media_type, data)}}
                    for media_type, data in images
                )
                converted.append({"role": "user", "content": parts})
            else:
                converted.append({"role": message.get("role", "user"), "content": text})
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": converted,
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        body = _post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            payload=payload,
            transport=self._transport,
        )
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise AiUnavailableError("模型服务的响应缺少 choices[0].message.content") from exc


class AnthropicProvider:
    """``/v1/messages``：system 单独一段，图片是 base64 source 块。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        images: Sequence[tuple[str, bytes]] = (),
        json_mode: bool = False,
    ) -> str:
        system = "\n\n".join(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        )
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "system":
                continue
            text = str(message.get("content", ""))
            if message.get("role") == "user" and images and message is messages[-1]:
                parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
                parts.extend(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }
                    for media_type, data in images
                )
                converted.append({"role": "user", "content": parts})
            else:
                converted.append({"role": message.get("role", "user"), "content": text})
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "messages": converted,
        }
        if system:
            payload["system"] = system
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        body = _post(
            f"{self._base_url}/v1/messages",
            headers=headers,
            payload=payload,
            transport=self._transport,
        )
        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise AiUnavailableError("模型服务的响应缺少 content 数组")
        return "".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict))


def build_provider(config: AIConfig) -> LLMProvider | None:
    if not config.enabled:
        return None
    base_url = config.resolved_base_url()
    model = config.resolved_model()
    if not base_url or not model or not config.api_key:
        raise AiNotConfiguredError(
            "AI 已开启但缺少配置：需要 DRIVER_NOTES_LLM_BASE_URL / _API_KEY / _MODEL"
        )
    if config.mode == "anthropic":
        return AnthropicProvider(base_url=base_url, api_key=config.api_key, model=model)
    return OpenAICompatProvider(base_url=base_url, api_key=config.api_key, model=model)


# --------------------------------------------------------------------- 补丁


def _strip_fence(text: str) -> str:
    match = _FENCE.search(text)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    return stripped


def parse_reply(text: str) -> dict[str, Any]:
    """把模型回复解析成 ``{"summary": str, "edits": [{"find","replace"}]}``。"""

    payload = _strip_fence(text)
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise AiReplyUnparsableError("模型回复不是 JSON，无法解析成补丁") from exc
    if not isinstance(data, dict):
        raise AiReplyUnparsableError("模型回复必须是 JSON 对象")
    summary = data.get("summary") or data.get("reply") or ""
    raw_edits = data.get("edits") or []
    if not isinstance(raw_edits, list):
        raise AiReplyUnparsableError("edits 必须是数组")
    edits: list[dict[str, str]] = []
    for item in raw_edits:
        if not isinstance(item, dict):
            raise AiReplyUnparsableError("edits 的每一项都必须是对象")
        find = item.get("find")
        replace = item.get("replace", "")
        if not isinstance(find, str) or not isinstance(replace, str):
            raise AiReplyUnparsableError("edits 的 find / replace 必须是字符串")
        edits.append({"find": find, "replace": replace})
    return {"summary": str(summary), "edits": edits}


def apply_edits(body: str, edits: Sequence[dict[str, str]]) -> tuple[str, list[int], list[int]]:
    """按顺序套补丁；``find`` 不是「恰好出现一次」就跳过，返回已应用/被跳过的下标。"""

    current = body
    applied: list[int] = []
    skipped: list[int] = []
    for index, edit in enumerate(edits):
        find = edit.get("find", "")
        replace = edit.get("replace", "")
        if find == "":
            current = f"{current}\n{replace}".strip() if current else replace
            applied.append(index)
            continue
        if current.count(find) != 1:
            skipped.append(index)
            continue
        current = current.replace(find, replace, 1)
        applied.append(index)
    return current, applied, skipped


# --------------------------------------------------------------------- 门面


class NoteAI:
    """把「拼上下文 → 调模型 → 解析 → 应用」收在一处，工具与 REST 都只调它。"""

    def __init__(self, config: AIConfig, provider: LLMProvider | None = None) -> None:
        self.config = config
        self._provider = provider

    @property
    def mode(self) -> str:
        return self.config.mode

    def _require(self) -> LLMProvider:
        if self._provider is None:
            if self.mode == "manual":
                raise AiNotConfiguredError(
                    "manual 模式不调用模型：用 note_summarize 拿上下文包，把结果粘贴回 "
                    "note_ingest_ai_reply"
                )
            raise AiNotConfiguredError(
                "AI 未启用：设 DRIVER_NOTES_LLM=openai|anthropic|manual 后重启"
            )
        return self._provider

    def _messages(self, note: dict[str, Any], annotations: Sequence[dict[str, Any]]) -> str:
        lines = [f"# {note['name']}", "", str(note.get("body_md") or "（正文为空）"), ""]
        if annotations:
            lines.append("## 批注")
            for annotation in annotations:
                lines.append(f"- [{annotation.get('kind')}] {annotation.get('body_md')}")
            lines.append("")
        images = note.get("images") or []
        if images:
            lines.append(f"（随消息附上 {len(images)} 张截图）")
        return "\n".join(lines)

    async def run(
        self,
        *,
        note: dict[str, Any],
        annotations: Sequence[dict[str, Any]],
        instruction: str,
        images: Sequence[tuple[str, bytes]] = (),
        history: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        provider = self._require()
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # 历史项来自两处：存储的消息（``body``）与调用方拼的 chat 形状（``content``）。
        messages.extend(
            {
                "role": str(item["role"]),
                "content": str(item.get("body") or item.get("content") or ""),
            }
            for item in history
        )
        messages.append(
            {
                "role": "user",
                "content": f"{self._messages(note, annotations)}\n\n## 要求\n{instruction}",
            }
        )
        reply = await provider.complete(messages, images=images, json_mode=True)
        parsed = parse_reply(reply)
        return {
            "mode": self.mode,
            "model": provider.model,
            "reply": reply,
            "summary": parsed["summary"],
            "edits": parsed["edits"],
        }

    def manual_bundle(
        self,
        *,
        note: dict[str, Any],
        annotations: Sequence[dict[str, Any]],
        instruction: str,
    ) -> dict[str, Any]:
        context = self._messages(note, annotations)
        prompt = f"{SYSTEM_PROMPT}\n\n{context}\n\n## 要求\n{instruction}"
        return {
            "mode": "manual",
            "note_id": note["id"],
            "copy_text": prompt,
            "image_dir": "媒体目录里按 sha256 取图，需手工附上",
            "images": [
                {
                    "capture_id": image["capture_id"],
                    "media_type": image["media_type"],
                    "path": image.get("path"),
                }
                for image in note.get("images") or []
            ],
        }

    async def act(
        self,
        *,
        note: dict[str, Any],
        annotations: Sequence[dict[str, Any]],
        instruction: str,
        images: Sequence[tuple[str, bytes]] = (),
        history: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """手动模式返回上下文包（不调模型），其余模式真的去调模型。"""

        if self.mode == "manual":
            return self.manual_bundle(note=note, annotations=annotations, instruction=instruction)
        return await self.run(
            note=note,
            annotations=annotations,
            instruction=instruction,
            images=images,
            history=history,
        )
