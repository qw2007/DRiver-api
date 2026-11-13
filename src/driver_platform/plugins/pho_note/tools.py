"""PhoNote 的工具表：截图缓冲区 + 自由文件树 + 笔记 + 批注 + 版本 + 导出 + AI。

每个工具的形状都一样（一个入参模型 + 一次存储调用），所以这里用「一张表 + 一个
通用工具类」而不是二十多个近乎相同的类：加一个工具只加一行。``manifest()`` 用实例
方法覆盖基类的 ``classmethod``（注册表是按实例调用的，与远程插件同一套做法）。

P1 补齐到 17 个（树/笔记/图片/批注/版本/导出），P2 再加 5 个 AI 工具。
"""

from __future__ import annotations

import base64
import binascii
import inspect
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from driver_platform.errors import InvalidArgumentsError
from driver_platform.plugins.pho_note.ai import NoteAI, apply_edits, parse_reply
from driver_platform.plugins.pho_note.store import Store
from driver_platform.plugins.pho_note.sync import SyncConfig
from driver_platform.plugins.pho_note.sync import pull as sync_pull
from driver_platform.plugins.pho_note.sync import push as sync_push
from driver_platform.plugins.pho_note.sync import status as sync_status
from driver_platform.tools.base import Tool, ToolManifest

#: 单张图的上限（base64 解码前）。壳上传的是长边 1600 的 WebP，正常 100–400 KB。
MAX_IMAGE_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------- 入参模型


class NoteTreeInput(BaseModel):
    include_body: bool = Field(default=False, description="是否附带每篇笔记的 Markdown 正文")


class NoteNodeCreateInput(BaseModel):
    parent_id: str | None = Field(default=None, description="父目录 id；省略表示根")
    kind: Literal["folder", "note"] = Field(description="folder=目录，note=笔记")
    name: str = Field(min_length=1, max_length=200, description="节点名，同层重名会自动加 (2)")
    body_md: str = Field(default="", description="笔记正文（Markdown）；目录必须留空")


class NoteNodeRenameInput(BaseModel):
    node_id: str = Field(min_length=1, description="要改名的节点 id（目录或笔记）")
    name: str = Field(min_length=1, max_length=200, description="新名字，同层唯一")


class NoteNodeMoveInput(BaseModel):
    node_id: str = Field(min_length=1, description="要移动的节点 id")
    parent_id: str | None = Field(default=None, description="目标父目录；省略表示移到根")
    position: int | None = Field(
        default=None, ge=0, description="排在同层的第几位；省略表示排到最后"
    )


class NoteNodeDeleteInput(BaseModel):
    node_id: str = Field(min_length=1, description="要删除的节点 id")
    recursive: bool = Field(default=False, description="节点下还有内容时必须为 true，否则拒绝")


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


class NoteListInput(BaseModel):
    query: str = Field(default="", max_length=200, description="按标题或正文搜；空串表示最近修改")
    limit: int = Field(default=50, ge=1, le=200, description="最多返回多少篇")


class NoteUpdateInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    name: str | None = Field(default=None, max_length=200, description="新标题；省略表示不改")
    body_md: str | None = Field(
        default=None, description="新正文；省略表示不改（改正文会存旧版本）"
    )
    reason: str = Field(default="edit", max_length=80, description="版本备注，如 edit / ai-summary")


class NoteSetImagesInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    capture_ids: list[str] = Field(
        max_length=50, description="按顺序重写整份图片列表；空列表表示摘掉所有图"
    )


class NoteAnnotateInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    kind: Literal["note", "text", "image"] = Field(
        description="note=整篇、text=文字锚点、image=图片矩形锚点"
    )
    anchor: dict[str, Any] = Field(
        default_factory=dict,
        description="锚点：整篇用 {}；文字用 {quote,prefix,suffix,offset}；图片用 {capture_id,rect}",
    )
    body_md: str = Field(min_length=1, max_length=4000, description="批注内容（Markdown）")
    color: str = Field(default="yellow", max_length=20, description="高亮色，UI 自己定义取值")


class NoteAnnotateDeleteInput(BaseModel):
    annotation_id: str = Field(min_length=1, description="批注 id")


class NoteRevisionsInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    limit: int = Field(default=20, ge=1, le=100, description="最多返回多少个旧版本")


class NoteRollbackInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    revision_id: str = Field(min_length=1, description="要回滚到的版本 id")


class EmptyInput(BaseModel):
    """没有参数的操作用它占位（备份/同步）。"""


class NoteExportInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")


class NoteSummarizeInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    instruction: str = Field(default="", description="额外要求；留空表示「整理成结构化要点」")


class NoteChatInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    message: str = Field(min_length=1, max_length=4000, description="想问或想让 AI 改什么")


class NoteMessagesInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    limit: int = Field(default=50, ge=1, le=200, description="最多返回多少条")


class NoteApplyPatchInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    find: str = Field(
        default="", description="正文里逐字出现且只出现一次的原文；给空串表示追加到末尾"
    )
    replace: str = Field(default="", description="替换成的文字")


class NoteIngestReplyInput(BaseModel):
    note_id: str = Field(min_length=1, description="笔记节点 id")
    reply: str = Field(min_length=1, description="粘贴回来的模型回复（JSON 补丁）")


# --------------------------------------------------------------------- 通用工具


class _FnTool(Tool[BaseModel, Any]):
    """一个工具 = 一段描述 + 一个入参模型 + 一次调用。"""

    contract_version = "1.0"
    output_model = None

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_model: type[BaseModel],
        handler: Callable[[Any], Any],
    ) -> None:
        self.name = name
        self.description = description
        self.input_model = input_model
        self._handler = handler

    def manifest(self) -> ToolManifest:  # type: ignore[override]
        return ToolManifest(
            name=self.name,
            contract_version=self.contract_version,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
        )

    async def execute(self, arguments: Any) -> Any:
        result = self._handler(arguments)
        if inspect.isawaitable(result):
            return await result
        return result


def _add_capture(store: Store) -> Callable[[NoteAddCaptureInput], Any]:
    def run(arguments: NoteAddCaptureInput) -> Any:
        try:
            data = base64.b64decode(arguments.image_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidArgumentsError("image_b64 不是合法的 base64") from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise InvalidArgumentsError(f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB 上限")
        return store.add_capture(
            data,
            source=arguments.source,
            app_title=arguments.app_title,
            width=arguments.width,
            height=arguments.height,
        )

    return run


def _note_with_images(
    store: Store, ai: NoteAI, note_id: str
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """取笔记 + 要发给模型的图片字节（张数由 ``DRIVER_NOTES_LLM_MAX_IMAGES`` 控制）。"""

    note = store.get_note(note_id)
    images: list[tuple[str, bytes]] = []
    for image in (note.get("images") or [])[: ai.config.max_images]:
        images.append(store.media_bytes(image["capture_id"]))
    return note, images


def _apply(reply: str, note: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_reply(reply)
    body, applied, skipped = apply_edits(note["body_md"], parsed["edits"])
    return {
        "summary": parsed["summary"],
        "body_md": body,
        "edits": parsed["edits"],
        "applied": applied,
        "skipped": skipped,
        "changed": body != note["body_md"],
    }


def build_tools(store: Store, ai: NoteAI, sync: SyncConfig | None = None) -> list[Tool[Any, Any]]:
    """按固定顺序返回工具表（顺序决定 Agent 看到的面板顺序）。"""

    config = sync or SyncConfig()

    def tool(
        name: str,
        description: str,
        input_model: type[BaseModel],
        handler: Callable[[Any], Any],
    ) -> Tool[Any, Any]:
        return _FnTool(name=name, description=description, input_model=input_model, handler=handler)

    return [
        # 树与笔记
        tool(
            "note_tree",
            "查看 PhoNote 的整棵文件树（目录与笔记，任意深度）",
            NoteTreeInput,
            lambda a: store.tree(include_body=a.include_body),
        ),
        tool(
            "note_node_create",
            "在文件树里新建一个目录或一篇空笔记（同层重名自动加 (2)）",
            NoteNodeCreateInput,
            lambda a: store.create_node(
                parent_id=a.parent_id, kind=a.kind, name=a.name, body_md=a.body_md
            ),
        ),
        tool(
            "note_node_rename",
            "给目录或笔记改名（同层唯一，撞名返回 node_name_conflict）",
            NoteNodeRenameInput,
            lambda a: store.rename_node(a.node_id, a.name),
        ),
        tool(
            "note_node_move",
            "把目录或笔记移动到另一个目录下（禁止移到自己或自己的子孙里）",
            NoteNodeMoveInput,
            lambda a: store.move_node(a.node_id, parent_id=a.parent_id, position=a.position),
        ),
        tool(
            "note_node_delete",
            "删除目录或笔记；节点下还有内容时要求 recursive=true",
            NoteNodeDeleteInput,
            lambda a: store.delete_node(a.node_id, recursive=a.recursive),
        ),
        # 图片缓冲区
        tool(
            "note_add_capture",
            "把一张截图放进图片缓冲区（内容寻址去重：同一张图只落一份盘）",
            NoteAddCaptureInput,
            lambda a: _add_capture(store)(a),
        ),
        tool(
            "note_list_captures",
            "列出图片缓冲区里的截图（默认只列还没被笔记用过的）",
            NoteListCapturesInput,
            lambda a: store.list_captures(limit=a.limit, unused_only=a.unused_only),
        ),
        # 笔记
        tool(
            "note_compose",
            "把缓冲区里选中的多张图组成为一篇笔记，放进指定目录",
            NoteComposeInput,
            lambda a: store.compose(
                parent_id=a.parent_id,
                capture_ids=a.capture_ids,
                title=a.title,
                body_md=a.body_md,
            ),
        ),
        tool(
            "note_get",
            "读一篇笔记：正文、元信息、按顺序挂着的图片与批注",
            NoteGetInput,
            lambda a: store.get_note(a.note_id),
        ),
        tool(
            "note_list",
            "按标题或正文搜索笔记（默认按最近修改倒序）",
            NoteListInput,
            lambda a: store.list_notes(query=a.query, limit=a.limit),
        ),
        tool(
            "note_update",
            "改笔记的标题或正文（改正文会把旧正文存成一个版本）",
            NoteUpdateInput,
            lambda a: store.update_note(a.note_id, name=a.name, body_md=a.body_md, reason=a.reason),
        ),
        tool(
            "note_set_images",
            "按给定顺序整体重写一篇笔记的图片列表（增、删、重排都走这里）",
            NoteSetImagesInput,
            lambda a: store.set_images(a.note_id, a.capture_ids),
        ),
        # 批注
        tool(
            "note_annotate",
            "给笔记加一条批注：整篇 / 文字锚点 / 图片矩形锚点",
            NoteAnnotateInput,
            lambda a: store.add_annotation(
                a.note_id, kind=a.kind, anchor=a.anchor, body_md=a.body_md, color=a.color
            ),
        ),
        tool(
            "note_annotate_delete",
            "删除一条批注",
            NoteAnnotateDeleteInput,
            lambda a: store.delete_annotation(a.annotation_id),
        ),
        # 版本与导出
        tool(
            "note_revisions",
            "列出笔记保存过的旧版本（最近在前）",
            NoteRevisionsInput,
            lambda a: store.list_revisions(a.note_id, limit=a.limit),
        ),
        tool(
            "note_rollback",
            "把笔记正文回滚到某个版本（回滚本身也会存一个版本）",
            NoteRollbackInput,
            lambda a: store.rollback(a.note_id, a.revision_id),
        ),
        tool(
            "note_export",
            "导出一篇笔记的 Markdown（图片用绝对路径，粘到哪里都还在）",
            NoteExportInput,
            lambda a: store.export_markdown(a.note_id),
        ),
        # AI（P2）：manual 模式不调模型，只把上下文包交回去，由人贴给网页版
        tool(
            "note_summarize",
            "让 AI 读笔记正文与截图，生成总结与增量修订建议（manual 模式返回上下文包）",
            NoteSummarizeInput,
            lambda a: run_summarize(store, ai, a.note_id, a.instruction),
        ),
        tool(
            "note_chat",
            "就这篇笔记与 AI 对话：带上最近十条历史，回复会记进讨论区",
            NoteChatInput,
            lambda a: run_chat(store, ai, a.note_id, a.message),
        ),
        tool(
            "note_messages",
            "读一篇笔记的讨论区消息（旧到新）",
            NoteMessagesInput,
            lambda a: store.list_messages(a.note_id, limit=a.limit),
        ),
        tool(
            "note_apply_patch",
            "把一处 find/replace 直接套用到笔记正文（find 必须逐字唯一；空串表示追加）",
            NoteApplyPatchInput,
            lambda a: run_patch(store, a.note_id, a.find, a.replace),
        ),
        tool(
            "note_ingest_ai_reply",
            "把从网页版模型粘回来的 JSON 回复解析并套用到正文",
            NoteIngestReplyInput,
            lambda a: _ingest(store, a),
        ),
        # 备份与同步（P3）
        tool(
            "note_backup",
            "把库快照和全部图片打成一个 zip 放进 backups/（换机器或离线恢复用）",
            EmptyInput,
            lambda a: store.backup_zip(),
        ),
        tool(
            "note_sync_push",
            "把图片缺口、库快照推给 WebDAV（单主设备：最后写 manifest 算提交）",
            EmptyInput,
            lambda a: sync_push(store, config),
        ),
        tool(
            "note_sync_pull",
            "用远端快照覆盖本地并补齐缺图（本地有未推送改动时拒绝）",
            EmptyInput,
            lambda a: sync_pull(store, config),
        ),
        tool(
            "note_sync_status",
            "看同步状态：远端是否配置、上次推/拉时间、本地有没有未推送改动",
            EmptyInput,
            lambda a: sync_status(store, config),
        ),
    ]


# --------------------------------------------------------------------- AI 处理器

_DEFAULT_INSTRUCTION = "把这篇笔记（连同截图里的板书）整理成结构化要点。"


async def run_summarize(store: Store, ai: NoteAI, note_id: str, instruction: str) -> dict[str, Any]:
    note, images = _note_with_images(store, ai, note_id)
    prompt = instruction.strip() or _DEFAULT_INSTRUCTION
    outcome = await ai.act(
        note=note,
        annotations=note.get("annotations") or [],
        instruction=prompt,
        images=images,
    )
    if outcome.get("mode") == "manual":
        return outcome
    store.add_message(note["id"], role="user", body=prompt)
    store.add_message(note["id"], role="assistant", body=outcome["summary"])
    return outcome


async def run_chat(store: Store, ai: NoteAI, note_id: str, message: str) -> dict[str, Any]:
    note, images = _note_with_images(store, ai, note_id)
    # 先把提问落库：模型不可用时这句也算数，用户回头能看到自己问过什么。
    store.add_message(note["id"], role="user", body=message)
    history = [
        {"role": item["role"], "content": item["body"]}
        for item in store.list_messages(note["id"], limit=10)
    ]
    outcome = await ai.act(
        note=note,
        annotations=note.get("annotations") or [],
        instruction=message,
        images=images,
        history=history,
    )
    if outcome.get("mode") == "manual":
        return outcome
    store.add_message(note["id"], role="assistant", body=outcome["summary"])
    return outcome


def run_patch(store: Store, note_id: str, find: str, replace: str) -> dict[str, Any]:
    note = store.get_note(note_id)
    body, applied, skipped = apply_edits(note["body_md"], [{"find": find, "replace": replace}])
    if not applied:
        # 没套上就别写库：免得白存一个「内容没变」的版本。
        return {
            "note_id": note["id"],
            "body_md": note["body_md"],
            "applied": applied,
            "skipped": skipped,
            "changed": False,
        }
    updated = store.update_note(note["id"], body_md=body, reason="ai-patch")
    return {
        "note_id": note["id"],
        "body_md": updated["body_md"],
        "applied": applied,
        "skipped": skipped,
        "changed": True,
    }


def _ingest(store: Store, arguments: NoteIngestReplyInput) -> dict[str, Any]:
    note = store.get_note(arguments.note_id)
    result = _apply(arguments.reply, note)
    if result["changed"]:
        updated = store.update_note(note["id"], body_md=result["body_md"], reason="ai-reply")
        result["body_md"] = updated["body_md"]
    store.add_message(note["id"], role="assistant", body=result["summary"])
    return result
