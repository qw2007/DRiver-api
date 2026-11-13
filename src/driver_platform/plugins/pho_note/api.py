"""PhoNote 的 REST 层：给桌面壳用的普通 JSON 接口（不套工具信封）。

挂不挂由 ``DRIVER_NOTES_UI`` 决定（默认关）：这是一个额外的、面向 UI 的暴露面，
和「工具清单」是两条独立的门。

作用域：读端点要 ``notes_read``，写端点要 ``notes_write``；全权钥匙（``scopes is None``，
含开放模式）都放行。这样「只读钥匙」``pho-ro:notes_read`` 就真的只能读——
工具侧那把只读钥匙是按**工具名**授权的，REST 侧按**读写**授权，两边各管各的。
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from driver_platform.errors import (
    InvalidArgumentsError,
    PermissionDeniedError,
    PluginNotFoundError,
)
from driver_platform.plugins.pho_note.ai import NoteAI
from driver_platform.plugins.pho_note.plugin import PhoNotePlugin
from driver_platform.plugins.pho_note.store import Store
from driver_platform.plugins.pho_note.sync import SyncConfig
from driver_platform.plugins.pho_note.sync import pull as sync_pull
from driver_platform.plugins.pho_note.sync import push as sync_push
from driver_platform.plugins.pho_note.sync import status as sync_status
from driver_platform.plugins.pho_note.tools import run_chat, run_patch, run_summarize
from driver_platform.security.http import PrincipalDep
from driver_platform.security.keys import Principal

NOTES_READ_SCOPE = "notes_read"
NOTES_WRITE_SCOPE = "notes_write"

#: 单张图上限（与工具侧同一个数字，两处都拦：REST 是壳的主路径）。
MAX_IMAGE_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------- 依赖


def _plugin(request: Request) -> PhoNotePlugin:
    host = getattr(request.app.state, "plugins", None)
    plugin = host.get("pho_note") if host is not None else None
    if not isinstance(plugin, PhoNotePlugin) or plugin.store is None:
        raise PluginNotFoundError("PhoNote 插件未装载")
    return plugin


def _store(request: Request) -> Store:
    # _plugin 已经挡住了「插件没装载」；cast 而不是 assert：断言在 -O 下会被抹掉，
    # 而这里要的是类型收窄，不是运行期检查。
    return cast(Store, _plugin(request).store)


def _ai(request: Request) -> NoteAI:
    ai = _plugin(request).ai
    if ai is None:
        raise PluginNotFoundError("PhoNote 插件未装载")
    return ai


def _guard(principal: Principal, scope: str) -> None:
    if principal.scopes is not None and scope not in principal.scopes:
        raise PermissionDeniedError(
            f"缺少作用域：{scope}",
            details=[{"type": "missing_scope", "loc": ["authorization"], "msg": scope}],
        )


def read_guard(principal: PrincipalDep) -> None:
    _guard(principal, NOTES_READ_SCOPE)


def write_guard(principal: PrincipalDep) -> None:
    _guard(principal, NOTES_WRITE_SCOPE)


StoreDep = Annotated[Store, Depends(_store)]
AiDep = Annotated[NoteAI, Depends(_ai)]


def _sync_config(request: Request) -> SyncConfig:
    return _plugin(request).sync


SyncDep = Annotated[SyncConfig, Depends(_sync_config)]
Read = Depends(read_guard)
Write = Depends(write_guard)


# --------------------------------------------------------------------- 请求体


class CreateNodeBody(BaseModel):
    model_config = {"extra": "forbid"}

    parent_id: str | None = None
    kind: Literal["folder", "note"]
    name: str = Field(min_length=1, max_length=200)
    body_md: str = ""
    template: Literal["lecture"] | None = None


class RenameNodeBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)


class MoveNodeBody(BaseModel):
    model_config = {"extra": "forbid"}

    parent_id: str | None = None
    position: int | None = Field(default=None, ge=0)


class UpdateNoteBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, max_length=200)
    body_md: str | None = None
    reason: str = Field(default="edit", max_length=80)


class ComposeBody(BaseModel):
    model_config = {"extra": "forbid"}

    parent_id: str | None = None
    capture_ids: list[str] = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=200)
    body_md: str = ""


class UploadCaptureBody(BaseModel):
    model_config = {"extra": "forbid"}

    image_b64: str = Field(min_length=1)
    source: str = Field(default="shell", max_length=40)
    app_title: str | None = Field(default=None, max_length=200)
    width: int = Field(default=0, ge=0, le=100_000)
    height: int = Field(default=0, ge=0, le=100_000)
    keep_original: bool = False


class SetImagesBody(BaseModel):
    model_config = {"extra": "forbid"}

    capture_ids: list[str] = Field(max_length=50)


class AnnotateBody(BaseModel):
    model_config = {"extra": "forbid"}

    kind: Literal["note", "text", "image"] = "note"
    anchor: dict[str, Any] = Field(default_factory=dict)
    body_md: str = Field(min_length=1, max_length=4000)
    color: str = Field(default="yellow", max_length=20)


class RollbackBody(BaseModel):
    model_config = {"extra": "forbid"}

    revision_id: str = Field(min_length=1)


class SummarizeBody(BaseModel):
    model_config = {"extra": "forbid"}

    instruction: str = Field(default="", max_length=2000)


class ChatBody(BaseModel):
    model_config = {"extra": "forbid"}

    message: str = Field(min_length=1, max_length=4000)


class ApplyBody(BaseModel):
    model_config = {"extra": "forbid"}

    find: str = Field(default="")
    replace: str = Field(default="")


# --------------------------------------------------------------------- 路由


def build_router() -> APIRouter:
    router = APIRouter(prefix="/notes/api", tags=["pho-note"])

    @router.get("/tree", dependencies=[Read])
    def tree(store: StoreDep) -> list[dict[str, Any]]:
        return store.tree()

    @router.post("/nodes", status_code=201, dependencies=[Write])
    def create_node(store: StoreDep, body: Annotated[CreateNodeBody, Body()]) -> dict[str, Any]:
        return store.create_node(**body.model_dump())

    @router.patch("/nodes/{node_id}", dependencies=[Write])
    def rename_node(
        store: StoreDep, node_id: str, body: Annotated[RenameNodeBody, Body()]
    ) -> dict[str, Any]:
        return store.rename_node(node_id, body.name)

    @router.post("/nodes/{node_id}/move", dependencies=[Write])
    def move_node(
        store: StoreDep, node_id: str, body: Annotated[MoveNodeBody, Body()]
    ) -> dict[str, Any]:
        return store.move_node(node_id, parent_id=body.parent_id, position=body.position)

    @router.delete("/nodes/{node_id}", dependencies=[Write])
    def delete_node(store: StoreDep, node_id: str, recursive: bool = False) -> dict[str, Any]:
        return store.delete_node(node_id, recursive=recursive)

    @router.get("/notes", dependencies=[Read])
    def list_notes(
        store: StoreDep,
        query: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        return store.list_notes(query=query, limit=limit)

    @router.post("/notes", status_code=201, dependencies=[Write])
    def compose(store: StoreDep, body: Annotated[ComposeBody, Body()]) -> dict[str, Any]:
        return store.compose(**body.model_dump())

    @router.get("/notes/{note_id}", dependencies=[Read])
    def get_note(store: StoreDep, note_id: str) -> dict[str, Any]:
        return store.get_note(note_id)

    @router.patch("/notes/{note_id}", dependencies=[Write])
    def update_note(
        store: StoreDep, note_id: str, body: Annotated[UpdateNoteBody, Body()]
    ) -> dict[str, Any]:
        return store.update_note(note_id, name=body.name, body_md=body.body_md, reason=body.reason)

    @router.put("/notes/{note_id}/images", dependencies=[Write])
    def set_images(
        store: StoreDep, note_id: str, body: Annotated[SetImagesBody, Body()]
    ) -> dict[str, Any]:
        return store.set_images(note_id, body.capture_ids)

    @router.post("/notes/{note_id}/annotations", status_code=201, dependencies=[Write])
    def annotate(
        store: StoreDep, note_id: str, body: Annotated[AnnotateBody, Body()]
    ) -> dict[str, Any]:
        return store.add_annotation(note_id, **body.model_dump())

    @router.delete("/annotations/{annotation_id}", dependencies=[Write])
    def delete_annotation(store: StoreDep, annotation_id: str) -> dict[str, Any]:
        return store.delete_annotation(annotation_id)

    @router.get("/notes/{note_id}/revisions", dependencies=[Read])
    def revisions(
        store: StoreDep, note_id: str, limit: Annotated[int, Query(ge=1, le=100)] = 20
    ) -> list[dict[str, Any]]:
        return store.list_revisions(note_id, limit=limit)

    @router.post("/notes/{note_id}/rollback", dependencies=[Write])
    def rollback(
        store: StoreDep, note_id: str, body: Annotated[RollbackBody, Body()]
    ) -> dict[str, Any]:
        return store.rollback(note_id, body.revision_id)

    @router.get("/notes/{note_id}/export", dependencies=[Read])
    def export(store: StoreDep, note_id: str) -> Response:
        exported = store.export_markdown(note_id)
        # 头只能是 latin-1：中文标题按 RFC 5987 编码，否则 Starlette 直接抛 UnicodeEncodeError。
        filename = quote(f"{exported['name']}.md", safe="")
        return Response(
            content=exported["markdown"],
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    @router.get("/notes/{note_id}/messages", dependencies=[Read])
    def messages(
        store: StoreDep, note_id: str, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[dict[str, Any]]:
        return store.list_messages(note_id, limit=limit)

    @router.post("/notes/{note_id}/summarize", dependencies=[Write])
    async def summarize(
        store: StoreDep, ai: AiDep, note_id: str, body: Annotated[SummarizeBody, Body()]
    ) -> dict[str, Any]:
        return await run_summarize(store, ai, note_id, body.instruction)

    @router.post("/notes/{note_id}/chat", dependencies=[Write])
    async def chat(
        store: StoreDep, ai: AiDep, note_id: str, body: Annotated[ChatBody, Body()]
    ) -> dict[str, Any]:
        return await run_chat(store, ai, note_id, body.message)

    @router.post("/notes/{note_id}/apply", dependencies=[Write])
    def apply_patch(
        store: StoreDep, note_id: str, body: Annotated[ApplyBody, Body()]
    ) -> dict[str, Any]:
        return run_patch(store, note_id, body.find, body.replace)

    @router.get("/captures", dependencies=[Read])
    def list_captures(
        store: StoreDep,
        limit: Annotated[int, Query(ge=1, le=200)] = 60,
        unused_only: bool = True,
    ) -> list[dict[str, Any]]:
        return store.list_captures(limit=limit, unused_only=unused_only)

    @router.post("/captures", status_code=201, dependencies=[Write])
    def upload_capture(
        store: StoreDep, body: Annotated[UploadCaptureBody, Body()]
    ) -> dict[str, Any]:
        try:
            data = base64.b64decode(body.image_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidArgumentsError("image_b64 不是合法的 base64") from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise InvalidArgumentsError(f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB 上限")
        return store.add_capture(
            data,
            source=body.source,
            app_title=body.app_title,
            width=body.width,
            height=body.height,
            keep_original=body.keep_original,
        )

    @router.get("/captures/{capture_id}/image", dependencies=[Read])
    def capture_image(store: StoreDep, capture_id: str) -> Response:
        capture = store.get_capture(capture_id)
        return Response(
            content=(store.root / capture["path"]).read_bytes(),
            media_type=capture["media_type"],
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    # 备份与同步（P3）

    @router.post("/backup", dependencies=[Write])
    def backup(store: StoreDep) -> dict[str, Any]:
        return store.backup_zip()

    @router.get("/sync/status", dependencies=[Read])
    def sync_state(store: StoreDep, config: SyncDep) -> dict[str, Any]:
        return sync_status(store, config)

    @router.post("/sync/push", dependencies=[Write])
    def sync_upload(store: StoreDep, config: SyncDep) -> dict[str, Any]:
        return sync_push(store, config)

    @router.post("/sync/pull", dependencies=[Write])
    def sync_download(store: StoreDep, config: SyncDep) -> dict[str, Any]:
        return sync_pull(store, config)

    return router
