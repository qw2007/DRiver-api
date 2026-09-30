"""MCP 适配器的翻译层：``ToolManifest`` → MCP ``Tool``、以及错误信封（不起进程）。

端到端（真进程 + 官方客户端）见 ``tests/test_mcp_server.py``。
这里只钉住纯函数行为，跑得快、失败时定位准。
"""

from __future__ import annotations

import json
from types import ModuleType

import pytest

from driver_platform.errors import ToolNotFoundError
from driver_platform.tools import ToolRegistry, register_builtin_tools

pytest.importorskip("mcp")  # mcp 是可选依赖（pip install driver-platform[mcp]）


def _adapter() -> ModuleType:
    """局部 import：整个仓库只有这个适配器能碰 mcp SDK，测试也一样晚加载。"""
    from driver_platform.protocols import mcp

    return mcp


@pytest.fixture
def echo_manifest():
    registry = ToolRegistry()
    register_builtin_tools(registry)
    return registry.get("echo").manifest()


def test_tool_translation_mirrors_the_manifest(echo_manifest) -> None:
    tool = _adapter().to_mcp_tool(echo_manifest)

    assert tool.name == echo_manifest.name
    assert tool.title == echo_manifest.name  # 客户端列表里显示的短名
    assert tool.description == echo_manifest.description
    assert tool.inputSchema == echo_manifest.input_schema
    assert tool.outputSchema == echo_manifest.output_schema


def test_success_result_exposes_structured_content(echo_manifest) -> None:
    result = _adapter().success_result({"echo": "hi", "times": 1})

    assert result.isError is False
    assert result.structuredContent == {"echo": "hi", "times": 1}
    assert json.loads(result.content[0].text) == {"echo": "hi", "times": 1}


def test_success_result_without_an_object_has_no_structured_content() -> None:
    """没有 ``output_model`` 的工具可以返回 ``None``：只给文本，不硬造对象。"""
    result = _adapter().success_result(None)

    assert result.isError is False
    assert result.structuredContent is None
    assert result.content[0].text == "null"


def test_error_result_reuses_the_http_envelope() -> None:
    result = _adapter().error_result(ToolNotFoundError("未注册的工具：ghost"))

    assert result.isError is True
    assert result.structuredContent == {
        "error": {"code": "tool_not_found", "message": "未注册的工具：ghost", "details": None}
    }
    assert json.loads(result.content[0].text) == result.structuredContent
