"""OpenAI 工具（function calling）Schema 适配器。

只做一件事：把 ``ToolManifest`` 翻译成 Chat Completions 的 ``tools`` 数组元素。

**为什么这个适配器不需要装 openai SDK**：导出工具 Schema 是**纯翻译**——
吃平台的 JSON Schema，吐 OpenAI 的形状，不调用任何模型。真去依赖 openai SDK，
就把「导出 Schema」和「调用模型」绑在一起了，而后者是 Agent 自己的事。

**为什么 ``contract_version`` 不出现在这里**：OpenAI 的函数定义只有
``name`` / ``description`` / ``parameters`` 三个位置，塞版本号要么污染描述、要么加私有字段
（严格模式的客户端会拒绝）。要版本就读阶段 1 的 ``GET /v1/tools``——那是平台的对外契约。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from driver_platform.tools import ToolManifest

__all__ = ["to_openai_tool", "to_openai_tools"]


def to_openai_tool(manifest: ToolManifest) -> dict[str, Any]:
    """单个工具 → OpenAI ``tools[]`` 元素。"""
    return {
        "type": "function",
        "function": {
            "name": manifest.name,
            "description": manifest.description,
            "parameters": manifest.input_schema,
        },
    }


def to_openai_tools(manifests: Iterable[ToolManifest]) -> list[dict[str, Any]]:
    """批量翻译，保持传入顺序（注册表已按工具名排序）。"""
    return [to_openai_tool(manifest) for manifest in manifests]
