"""「适配器隔离」不是口号：这条测试直接扫源码里的 import。

原则要求 MCP / OpenAI / 以后的 LangChain 适配器互不依赖。人能忘、review 会漏，
所以把它钉成测试：只有 ``protocols/mcp.py`` 与它的 stdio 入口可以 import mcp SDK，
核心服务（tools / api / bootstrap / main）与 OpenAI 适配器一律不许碰。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "driver_platform"

#: 允许 import mcp SDK 的文件（相对 SRC 的 posix 路径）——注意是「允许」，不是「必须」。
MCP_IMPORTERS = {"protocols/mcp.py", "protocols/mcp_server.py"}

#: 与协议适配无关、因此绝不该被适配器 import 的第三方 SDK 根名。
FORBIDDEN_IN_OPENAI_ADAPTER = {"mcp", "openai", "anthropic", "langchain"}


def _imported_modules(path: Path) -> set[str]:
    """一个文件里所有 import 的完整模块名（含函数内的延迟 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_only_the_mcp_adapter_imports_the_sdk() -> None:
    importers = {
        path.relative_to(SRC).as_posix()
        for path in _source_files()
        if any(module.split(".")[0] == "mcp" for module in _imported_modules(path))
    }
    assert importers <= MCP_IMPORTERS
    assert "protocols/mcp.py" in importers  # 适配器本体必须在名单里


def test_openai_adapter_is_pure_translation() -> None:
    modules = _imported_modules(SRC / "protocols" / "openai_schema.py")
    assert {module.split(".")[0] for module in modules} & FORBIDDEN_IN_OPENAI_ADAPTER == set()
    assert not any(module.startswith("driver_platform.protocols.mcp") for module in modules)


def test_core_does_not_depend_on_protocol_adapters() -> None:
    """核心服务不知道有具体适配器：反转依赖会让「加一家协议」变成改核心代码。

    ``driver_platform.protocols`` 这个包本身（开关 + ``build_router`` 工厂）是允许的：
    那是协议层对外的公共面，核心只需要知道「有没有开」和「把路由挂上」。
    """
    core = [SRC / "main.py", SRC / "bootstrap.py", SRC / "config.py", *SRC.rglob("api/*.py")]
    for path in core:
        modules = _imported_modules(path)
        assert not any(module.startswith("driver_platform.protocols.") for module in modules), path
