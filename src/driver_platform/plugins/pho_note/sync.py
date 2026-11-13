"""PhoNote 的 WebDAV 同步与备份。

设计文档 §7 的落地，**只做单主设备**：

- 远端布局 ``<url>/phonote/{manifest.json, db/<时间戳>-<sha8>.db, media/ab/cd/<sha>.<ext>}``；
- ``manifest.json`` 是提交点：**最后写**。它没更新，远端就还是旧状态；
- 媒体文件内容寻址、只传远端缺的那些；数据库快照走 ``VACUUM INTO``，保留 ``keep`` 份；
- push 记下「这次推上去的库 sha256」到本地 ``sync.json``；pull 前拿它比对当前库，
  不一致说明本地有没推的改动 → 直接拒绝（不做字段级双向合并，那是不归路的开始）。

HTTP 走 ``httpx``（与 ``remote.py``、``ai.py`` 一样的出网白名单成员）：每次调用新建
客户端，不共享连接池——同步是低频操作，连接复用不值一个常驻对象。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from driver_platform.errors import SyncFailedError, SyncNotConfiguredError
from driver_platform.plugins.pho_note.store import Store, _write_atomically

DEFAULT_KEEP = 5
DEFAULT_TIMEOUT = 30.0
MANIFEST_VERSION = 1
PREFIX = "phonote"
STATE_NAME = "sync.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class SyncConfig:
    """WebDAV 目标。``url`` 为空 = 没配置（工具会报 ``sync_not_configured``）。"""

    url: str = ""
    user: str = ""
    password: str = ""
    keep: int = DEFAULT_KEEP

    @property
    def enabled(self) -> bool:
        return bool(self.url.strip())


class WebDAVClient:
    """够用的 WebDAV 子集：MKCOL / PUT / GET / DELETE（不解析 PROPFIND 的 XML）。"""

    def __init__(
        self,
        config: SyncConfig,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.Client:
        auth = (
            httpx.BasicAuth(self._config.user, self._config.password) if self._config.user else None
        )
        return httpx.Client(
            base_url=self._config.url.rstrip("/") + "/",
            auth=auth,
            timeout=self._timeout,
            transport=self._transport,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            with self._client() as client:
                return client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SyncFailedError(f"WebDAV 连不上（{type(exc).__name__}）") from exc

    def mkcol(self, path: str) -> None:
        response = self._request("MKCOL", path)
        # 已存在返回 405，某些服务器返回 301/409；都当成功，幂等。
        if response.status_code not in (201, 301, 405, 409):
            raise SyncFailedError(f"WebDAV 建目录失败：{path} → HTTP {response.status_code}")

    def put(
        self, path: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        response = self._request("PUT", path, content=data, headers={"Content-Type": content_type})
        if response.status_code not in (200, 201, 204):
            raise SyncFailedError(f"WebDAV 上传失败：{path} → HTTP {response.status_code}")

    def get(self, path: str) -> bytes | None:
        response = self._request("GET", path)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SyncFailedError(f"WebDAV 下载失败：{path} → HTTP {response.status_code}")
        return response.content

    def delete(self, path: str) -> None:
        response = self._request("DELETE", path)
        if response.status_code not in (200, 204, 404):
            raise SyncFailedError(f"WebDAV 删除失败：{path} → HTTP {response.status_code}")


# ---------------------------------------------------------------- 本地同步状态


def state_path(store: Store) -> Path:
    return store.root / STATE_NAME


def read_state(store: Store) -> dict[str, Any]:
    path = state_path(store)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(store: Store, **fields: Any) -> dict[str, Any]:
    merged = {**read_state(store), **fields}
    _write_atomically(
        state_path(store), json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
    )
    return merged


# ---------------------------------------------------------------- 推送 / 拉取


def _client_for(config: SyncConfig, transport: httpx.BaseTransport | None) -> WebDAVClient:
    if not config.enabled:
        raise SyncNotConfiguredError(
            "WebDAV 未配置：设 DRIVER_NOTES_WEBDAV_URL（可选 _USER/_PASSWORD）后重启"
        )
    return WebDAVClient(config, transport=transport)


def _manifest(client: WebDAVClient) -> dict[str, Any] | None:
    blob = client.get(f"{PREFIX}/manifest.json")
    if blob is None:
        return None
    try:
        loaded = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise SyncFailedError("远端 manifest.json 不是合法 JSON") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("db"), str):
        raise SyncFailedError("远端 manifest.json 缺少 db 字段")
    return loaded


def _media_entry(entry: Mapping[str, Any]) -> tuple[str, str]:
    sha = entry.get("sha256")
    path = entry.get("path")
    if not isinstance(sha, str) or not isinstance(path, str):
        raise SyncFailedError("远端 manifest 的 media 条目缺少 sha256/path")
    return sha, path


def push(
    store: Store,
    config: SyncConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """把媒体缺口、库快照、最后 manifest 依次推上去。"""
    client = _client_for(config, transport)
    for directory in (PREFIX, f"{PREFIX}/db", f"{PREFIX}/media"):
        client.mkcol(directory)

    previous = _manifest(client) or {}
    remote_media = {_media_entry(item)[0] for item in previous.get("media", [])}
    local_media = store.media_files()

    uploaded = 0
    for item in local_media:
        if item["sha256"] in remote_media:
            continue
        blob = (store.root / item["path"]).read_bytes()
        client.put(f"{PREFIX}/{item['path']}", blob, content_type=item["media_type"])
        uploaded += 1

    snapshot = store.snapshot_bytes()
    digest = _sha256(snapshot)
    name = f"db/{_stamp()}-{digest[:8]}.db"
    client.put(f"{PREFIX}/{name}", snapshot)

    snapshots = [
        name,
        *[s for s in previous.get("snapshots", []) if isinstance(s, str) and s != name],
    ]
    pruned = snapshots[config.keep :]
    snapshots = snapshots[: config.keep]
    for old in pruned:
        client.delete(f"{PREFIX}/{old}")

    manifest = {
        "version": MANIFEST_VERSION,
        "db": name,
        "db_sha256": digest,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "media": [{"sha256": item["sha256"], "path": item["path"]} for item in local_media],
        "snapshots": snapshots,
    }
    # 最后才写 manifest：它一落地，远端就算「这次推送成功」。
    client.put(
        f"{PREFIX}/manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json",
    )

    write_state(
        store,
        pushed_db_sha256=digest,
        pushed_fingerprint=store.fingerprint(),
        pushed_at=manifest["created_at"],
        snapshot=name,
    )
    return {
        "pushed": True,
        "snapshot": name,
        "db_sha256": digest,
        "uploaded_media": uploaded,
        "media_total": len(local_media),
        "pruned": pruned,
    }


def pull(
    store: Store,
    config: SyncConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """拿远端快照覆盖本地，并把本地缺的媒体补齐。"""
    client = _client_for(config, transport)
    manifest = _manifest(client)
    if manifest is None:
        raise SyncFailedError("远端还没有备份：先 push 一次")

    state = read_state(store)
    pushed = state.get("pushed_fingerprint")
    if isinstance(pushed, str) and pushed and store.fingerprint() != pushed:
        raise SyncFailedError("本地有未推送的改动：先 push 再 pull（本工具不做双向合并）")

    name = manifest["db"]
    blob = client.get(f"{PREFIX}/{name}")
    if blob is None:
        raise SyncFailedError(f"远端快照不见了：{name}")
    digest = _sha256(blob)
    if digest != manifest.get("db_sha256"):
        raise SyncFailedError("远端快照校验失败：sha256 与 manifest 不一致")

    backup = store.backup_db()
    store.close()
    # WAL 的 -wal/-shm 是旧库的，必须一起清掉，否则新库会带上旧日志。
    for suffix in ("-wal", "-shm"):
        sidecar = store.db_path.with_name(store.db_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    _write_atomically(store.db_path, blob)
    store.reload()

    fetched = 0
    for entry in manifest.get("media", []):
        sha, path = _media_entry(entry)
        target = store.root / path
        if target.is_file():
            continue
        media = client.get(f"{PREFIX}/{path}")
        if media is None:
            raise SyncFailedError(f"远端缺图片：{path}")
        if _sha256(media) != sha:
            raise SyncFailedError(f"图片校验失败：{path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(target, media)
        fetched += 1

    write_state(
        store,
        pushed_db_sha256=manifest.get("db_sha256"),
        pushed_fingerprint=store.fingerprint(),
        pulled_at=_stamp(),
        snapshot=name,
    )
    return {
        "pulled": True,
        "snapshot": name,
        "db_sha256": manifest.get("db_sha256"),
        "backup": str(backup),
        "fetched_media": fetched,
        "media_total": len(manifest.get("media", [])),
    }


def status(store: Store, config: SyncConfig) -> dict[str, Any]:
    state = read_state(store)
    fingerprint = store.fingerprint()
    pushed = state.get("pushed_fingerprint")
    return {
        "configured": config.enabled,
        "remote": _remote_label(config.url),
        "keep": config.keep,
        "snapshot": state.get("snapshot"),
        "pushed_at": state.get("pushed_at"),
        "pulled_at": state.get("pulled_at"),
        "pushed_db_sha256": state.get("pushed_db_sha256"),
        "dirty": bool(pushed) and pushed != fingerprint,
        "media_total": len(store.media_files()),
    }


def _remote_label(url: str) -> str:
    """只回 host，不回完整 URL（用户/密码可能在 URL 里）。"""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0]
