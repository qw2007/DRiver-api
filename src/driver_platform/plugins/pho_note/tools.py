"""PhoNote 的六个工具（P0）：看树、建节点、收截图、看缓冲区、组笔记、读笔记。

P0 刻意只做「截图进缓冲区 → 组成笔记 → 读出来」这条闭环。改名/移动/删除、
批注、版本、AI、同步都是后面阶段的事，工具表在这里只列当前真能兑现的能力——
清单里出现一个调不通的工具，比少一个更糟。
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Literal

from pydantic import BaseModel, Field

from driver_platform.errors import InvalidArgumentsError
from driver_platform.plugins.pho_note.store import Store
from driver_platform.tools.base import Tool

#: 单张图的上限（base64 解码前）。壳上传的是长边 1600 的 WebP，正常 100–400 KB。
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class NoteTreeInput(BaseModel):
    """看整棵树。``include_body`` 打开时把笔记正文也带出来（Agent 一次拿全上下文）。"""

    include_body: bool = Field(default=False, description="是否附带每篇笔记的 Markdown 正文")


class NoteNodeCreateInput(BaseModel):
    parent_id: str | None = Field(default=None, description="父目录 id；省略表示根")
    kind: Literal["folder", "note"] = Field(description="folder=目录，note=笔记")
    name: str = Field(min_length=1, max_length=200, description="节点名，同层重名会自动加 (2)")
    body_md: str = Field(default="", description="笔记正文（Markdown）；目录必须留空")


class NoteAddCaptureInput(BaseModel):
    image_b64: str = Field(min_length=1, description="图片字节的 base64（PNG / JPEG / WebP）")
    source: str = Field(default="shell", max_length=40, description="来源标记，如 shell / agent")
    app_title: str | None = Field(default=None, max_length=200, description="截图时前台窗口标题")
    width: int = Field(default=0, ge=0, le=100_000, description="图片宽（像素，0 表示未知）")
    height: int = Field(default=0, ge=0, le=100_000, description="图片高（像素，0 表示未知）")


class NoteListCapturesInput(BaseModel):
    limit: int = Field(default=50, ge=1, le=200, description="最多返回多少张")
    unused_only: bool = Field(
        default=True, description="只列还没被任何笔记用过的图（图片区的默认视图）"
    )


class NoteComposeInput(BaseModel):
    parent_id: str | None = Field(default=None, description="把新笔记放在哪个目录下；省略表示根")
    capture_ids: list[str] = Field(
        min_length=1, max_length=50, description="按顺序组成笔记的图片 id"
    )
    title: str = Field(min_length=1, max_length=200, description="笔记标题（= 树里的节点名）")
    body_md: str = Field(default="", description="笔记正文（Markdown），可留空后由 AI 填")


class NoteGetInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")


class NoteTreeTool(Tool[NoteTreeInput, Any]):
    name = "note_tree"
    contract_version = "1.0"
    description = "查看 PhoNote 的整棵文件树（目录与笔记，任意深度）"
    input_model = NoteTreeInput
    output_model = None

    def __init__(self, store: Store) -> None:
        self._store = store

    async def execute(self, arguments: NoteTreeInput) -> Any:
        return self._store.tree(include_body=arguments.include_body)


class NoteNodeCreateTool(Tool[NoteNodeCreateInput, Any]):
    name = "note_node_create"
    contract_version = "1.0"
    description = "在文件树里新建一个目录或一篇空笔记（同层重名自动加 (2)）"
    input_model = NoteNodeCreateInput
    output_model = None

    def __init__(self, store: Store) -> None:
        self._store = store

    async def execute(self, arguments: NoteNodeCreateInput) -> Any:
        return self._store.create_node(
            parent_id=arguments.parent_id,
            kind=arguments.kind,
            name=arguments.name,
            body_md=arguments.body_md,
        )


class NoteAddCaptureTool(Tool[NoteAddCaptureInput, Any]):
    name = "note_add_capture"
    contract_version = "1.0"
    description = "把一张截图放进图片缓冲区（内容寻址去重：同一张图只落一份盘）"
    input_model = NoteAddCaptureInput
    output_model = None

    def __init__(self, store: Store) -> None:
        self._store = store

    async def execute(self, arguments: NoteAddCaptureInput) -> Any:
        try:
            data = base64.b64decode(arguments.image_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidArgumentsError("image_b64 不是合法的 base64") from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise InvalidArgumentsError(f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB 上限")
        return self._store.add_capture(
            data,
            source=arguments.source,
            app_title=arguments.app_title,
            width=arguments.width,
            height=arguments.height,
        )


class NoteListCapturesTool(Tool[NoteListCapturesInput, Any]):
    name = "note_list_captures"
    contract_version = "1.0"
    description = "列出图片缓冲区里的截图（默认只列还没被笔记用过的）"
    input_model = NoteListCapturesInput
    output_model = None

    def __init__(self, store: Store) -> None:
        self._store = store

    async def execute(self, arguments: NoteListCapturesInput) -> Any:
        return self._store.list_captures(limit=arguments.limit, unused_only=arguments.unused_only)


class NoteComposeTool(Tool[NoteComposeInput, Any]):
    name = "note_compose"
    contract_version = "1.0"
    description = "把缓冲区里选中的多张图组成为一篇笔记，放进指定目录"
    input_model = NoteComposeInput
    output_model = None

    def __init__(self, store: Store) -> None:
        self._store = store

    async def execute(self, arguments: NoteComposeInput) -> Any:
        return self._store.compose(
            parent_id=arguments.parent_id,
            capture_ids=arguments.capture_ids,
            title=arguments.title,
            body_md=arguments.body_md,
        )


class NoteGetTool(Tool[NoteGetInput, Any]):
    name = "note_get"
    contract_version = "1.0"
    description = "读一篇笔记：正文、元信息，以及按顺序挂着的图片"
    input_model = NoteGetInput
    output_model = None

    def __init__(self, store: Store) -> None:
        self._store = store

    async def execute(self, arguments: NoteGetInput) -> Any:
        return self._store.get_note(arguments.note_id)


def build_tools(store: Store) -> list[Tool[Any, Any]]:
    """按固定顺序返回 P0 的六个工具（工具清单的顺序决定 Agent 看到的面板顺序）。"""
    return [
        NoteTreeTool(store),
        NoteNodeCreateTool(store),
        NoteAddCaptureTool(store),
        NoteListCapturesTool(store),
        NoteComposeTool(store),
        NoteGetTool(store),
    ]
