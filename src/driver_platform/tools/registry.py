"""工具注册表：进程内唯一真相。

阶段 1 只有进程内注册；阶段 2 的插件加载、阶段 6 的远程注册都往这里写，
注册表本身不关心工具从哪来——这是「适配器隔离」的落点。
"""

from __future__ import annotations

from typing import Any

from driver_platform.errors import (
    DuplicateToolError,
    InvalidToolError,
    ToolNotFoundError,
)
from driver_platform.tools.base import (
    CONTRACT_VERSION_PATTERN,
    TOOL_NAME_PATTERN,
    Tool,
    ToolManifest,
)


class ToolRegistry:
    """工具名 → 工具实例。注册失败一律显式报错，不做静默覆盖。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}

    def register(self, tool: Tool[Any, Any]) -> None:
        self._validate(tool)
        if tool.name in self._tools:
            raise DuplicateToolError(f"工具名重复：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[Any, Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"未注册的工具：{name}") from None

    def manifests(self) -> list[ToolManifest]:
        return [tool.manifest() for _, tool in sorted(self._tools.items())]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    @staticmethod
    def _validate(tool: Tool[Any, Any]) -> None:
        if not TOOL_NAME_PATTERN.match(getattr(tool, "name", "")):
            raise InvalidToolError(
                f"工具名非法：{tool.name!r}，须匹配 {TOOL_NAME_PATTERN.pattern}",
            )
        if not CONTRACT_VERSION_PATTERN.match(getattr(tool, "contract_version", "")):
            raise InvalidToolError(
                f"契约版本非法：{tool.contract_version!r}，须形如 MAJOR.MINOR",
            )
        if not isinstance(getattr(tool, "input_model", None), type):
            raise InvalidToolError(f"工具 {tool.name} 缺少 input_model")
