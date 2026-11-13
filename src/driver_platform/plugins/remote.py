"""远程插件：远端用 HTTP 讲我们的契约，平台只持有一个 URL。

远端必须实现两个端点（契约 ``1.0``）::

    GET  {base_url}/manifest
         → {"plugin_id": "crm", "version": "1.0.0", "description": "...",
            "tools": [{"name": "find_customer", "contract_version": "1.0",
                       "description": "...", "input_schema": {...}}]}

    POST {base_url}/execute   {"tool": "find_customer", "arguments": {...}}
         → 200 {"tool": "find_customer", "contract_version": "1.0", "result": {...}}
         → 失败：我们的错误信封 {"error": {"code", "message", "details"}}

三条刻意的边界：

* **平台不下载、不执行远端代码**：注册一个远程插件只是「记住一个 URL」，风险面是
  SSRF 而不是远程代码执行——所以它值得一个独立的开关（默认关）与独立的管理作用域。
* **平台不复刻远端的入参校验**：``input_schema`` 原样取自远端清单（Agent 看到的仍是
  远端自己声明的精确契约），校验责任留在拥有实现的一方；远端返回的错误信封连同它
  自己的 ``code`` 一起透出，调用方不需要学第二套词汇。
* **清单只在装载时拉一次**：``Plugin.setup()`` 是同步接口，且既可能在启动期（没有事件
  循环）被调用、也可能在运行时注册请求里被调用——同步客户端两处都对。工具调用走异步
  客户端，且每次调用新建一个：``teardown()`` 同样是同步的，关不掉 ``AsyncClient``
  （这条捷径连同代价记在 ``docs/stages/stage-6.md`` 的已知限制里）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from driver_platform.errors import (
    PlatformError,
    RemoteToolFailedError,
    RemoteUnavailableError,
    sanitize_validation_errors,
)
from driver_platform.observability.context import outbound_trace_headers
from driver_platform.plugins.base import PLUGIN_VERSION_PATTERN, Plugin
from driver_platform.tools.base import Tool, ToolManifest

MANIFEST_PATH = "/manifest"
EXECUTE_PATH = "/execute"

DEFAULT_TIMEOUT = 5.0
"""单次远程请求的超时秒数（``DRIVER_REMOTE_TIMEOUT`` 可覆盖）。"""

#: 清单缓存时长。注册与重连都会读清单，60 秒内重复读同一份即可——
#: 远端改了工具表要等这一分钟，换来的是「重启平台不打远端一串请求」。
MANIFEST_CACHE_SECONDS = 60.0

#: 连接层重试次数（含首次）。只重试**连接没建立**的错误：那种情况下请求根本没到
#: 远端，重试不会把一次写操作执行两遍。超时与 5xx 不重试——远端慢了/坏了，重试只会更慢。
RETRY_ATTEMPTS = 2
RETRY_DELAY = 0.2

#: 远端错误码 → 平台 HTTP 状态。只映射「调用方自己改一改就能成」的错，
#: 其余（远端内部炸了、认不出的 code）一律 502：那是远端的可用性问题。
_REMOTE_CODE_STATUS = {
    "invalid_request": 422,
    "invalid_arguments": 422,
    "tool_not_found": 404,
    "permission_denied": 403,
}


def normalize_base_url(raw: str) -> str:
    """规整远端地址：去掉尾斜杠，只接受带主机的 ``http(s)``。

    不合规抛 ``ValueError``——两处调用方各自决定它是什么错：启动期配置 → ``invalid_configuration``，
    运行时注册请求 → 请求体校验失败（``invalid_request`` 422）。
    """
    url = raw.strip().rstrip("/")
    scheme, separator, rest = url.partition("://")
    host = rest.split("/", 1)[0]
    if not separator or scheme not in {"http", "https"} or not host:
        raise ValueError("远端地址必须是带主机的 http:// 或 https:// URL")
    return url


class RemoteArguments(BaseModel):
    """远程工具的入参：**不校验**，只保证是个 JSON 对象。

    校验器在远端（它是 schema 的拥有者）。平台要是自己实现一份，就得引入
    ``jsonschema`` 并保证两份真话永不漂移——这买卖不划算。
    """

    model_config = ConfigDict(extra="allow")


class RemoteToolSpec(BaseModel):
    """远端清单里的一个工具。字段写错就装载失败，契约就是契约。"""

    name: str
    contract_version: str = "1.0"
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class RemoteManifest(BaseModel):
    """远端清单。``version`` 是插件实现版本（``MAJOR.MINOR.PATCH``，宿主会校验）。"""

    plugin_id: str
    version: str
    description: str = ""
    tools: list[RemoteToolSpec]


class RemoteClient:
    """一个远程插件的 HTTP 访问点。"""

    def __init__(
        self,
        plugin_id: str,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        #: 测试注入点：``httpx.MockTransport`` 同时实现同步与异步两套协议，一个字段够用。
        self._transport = transport
        self._manifest_cache: tuple[float, RemoteManifest] | None = None

    def fetch_manifest(self) -> RemoteManifest:
        """拉清单（同步）。只在装载时跑一次，所以它阻塞一小会儿是可以接受的。"""
        cached = self._manifest_cache
        if cached is not None and time.monotonic() - cached[0] < MANIFEST_CACHE_SECONDS:
            return cached[1]
        url = f"{self.base_url}{MANIFEST_PATH}"
        attempt = 0
        while True:
            attempt += 1
            try:
                with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                    response = client.get(url, headers=_trace_headers())
                break
            except httpx.HTTPError as exc:
                # GET 是幂等的：连接失败与超时都可以安全重试一次。
                if attempt >= RETRY_ATTEMPTS:
                    raise self._unreachable(exc) from exc
                time.sleep(RETRY_DELAY)

        payload = self._decoded(response)
        try:
            manifest = RemoteManifest.model_validate(payload)
        except ValidationError as exc:
            raise RemoteToolFailedError(
                f"远程插件 {self.plugin_id} 的清单不符合契约",
                details=sanitize_validation_errors(exc.errors()),
            ) from exc
        if manifest.plugin_id != self.plugin_id:
            raise RemoteToolFailedError(
                f"远端清单自称 {manifest.plugin_id}，与注册的 id {self.plugin_id} 不一致"
            )
        if not manifest.tools:
            raise RemoteToolFailedError(f"远程插件 {self.plugin_id} 的清单里没有任何工具")
        self._manifest_cache = (time.monotonic(), manifest)
        return manifest

    async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """调用远端工具。入参原样转发——远端自己校验、自己报错。"""
        body: dict[str, Any] = {"tool": tool, "arguments": dict(arguments)}
        url = f"{self.base_url}{EXECUTE_PATH}"
        attempt = 0
        while True:
            attempt += 1
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, transport=self._transport
                ) as client:
                    response = await client.post(url, json=body, headers=_trace_headers())
                break
            except httpx.HTTPError as exc:
                # 只有「连接没建立」才重试：那种情况下请求没到远端，重试不会重复执行。
                if attempt >= RETRY_ATTEMPTS or not _is_connect_failure(exc):
                    raise self._unreachable(exc) from exc
                await asyncio.sleep(RETRY_DELAY)

        payload = self._decoded(response)
        if not isinstance(payload, Mapping):
            raise RemoteToolFailedError(f"远程插件 {self.plugin_id} 的响应不是 JSON 对象")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise RemoteToolFailedError(f"远程插件 {self.plugin_id} 的响应缺少对象形式的 result")
        return dict(result)

    def _decoded(self, response: httpx.Response) -> Any:
        """解码响应体；HTTP 错误码走信封解析。"""
        try:
            payload = response.json()
        except ValueError as exc:
            raise RemoteToolFailedError(
                f"远程插件 {self.plugin_id} 返回了非 JSON 响应（HTTP {response.status_code}）"
            ) from exc
        if response.status_code >= 400:
            raise self._remote_error(response.status_code, payload)
        return payload

    def _remote_error(self, status: int, payload: Any) -> PlatformError:
        """远端信封 → 平台错误：``code`` 原样透出，状态码按上表映射。"""
        envelope = payload.get("error") if isinstance(payload, Mapping) else None
        if not isinstance(envelope, Mapping):
            return RemoteToolFailedError(
                f"远程插件 {self.plugin_id} 返回了非契约的错误响应（HTTP {status}）"
            )
        code = envelope.get("code")
        if not isinstance(code, str) or not code:
            return RemoteToolFailedError(
                f"远程插件 {self.plugin_id} 返回了非契约的错误响应（HTTP {status}）"
            )
        message = envelope.get("message")
        details = envelope.get("details")
        return RemoteToolFailedError(
            f"远程插件 {self.plugin_id}：{message if isinstance(message, str) and message else code}",
            code=code,
            status_code=_REMOTE_CODE_STATUS.get(code, 502),
            details=details if isinstance(details, list) else None,
        )

    def _unreachable(self, exc: httpx.HTTPError) -> RemoteUnavailableError:
        """不回显内网地址：调用方只需要知道「远端不可用」，地址留在服务端配置里。"""
        return RemoteUnavailableError(f"远程插件 {self.plugin_id} 不可达：{type(exc).__name__}")


class RemoteTool(Tool[RemoteArguments, Any]):
    """远程工具的平台侧代理：入参透传、结果透传、说明书照抄远端。"""

    input_model = RemoteArguments
    output_model = None
    contract_version = "1.0"

    def __init__(self, spec: RemoteToolSpec, client: RemoteClient) -> None:
        self.name = spec.name
        self.contract_version = spec.contract_version
        self.description = spec.description
        self._input_schema = spec.input_schema
        self._client = client

    def manifest(self) -> ToolManifest:  # type: ignore[override]
        """照抄远端的 ``input_schema``——远端才是它的拥有者。

        因此这里必须是**实例**方法（基类的 ``manifest()`` 是类方法，靠 ``input_model``
        生成 schema）；注册表与宿主都是拿到实例再调 ``tool.manifest()``，所以这一层覆盖
        对调用方不可见。``output_model = None`` 让远端结果字典原样透出（见 ``tools/executor.py``）。
        """
        return ToolManifest(
            name=self.name,
            contract_version=self.contract_version,
            description=self.description,
            input_schema=self._input_schema,
        )

    async def execute(self, arguments: RemoteArguments) -> Any:
        return await self._client.call_tool(self.name, arguments.model_dump())


class RemotePlugin(Plugin):
    """一个远端服务的平台侧身份：装载时拉清单，卸载时没有资源要关。"""

    def __init__(
        self,
        plugin_id: str,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.base_url = normalize_base_url(base_url)
        self.version = "0.0.0"  # 占位：真实版本由远端清单声明（装载时覆盖）
        self.description = ""
        self._tools: list[Tool[Any, Any]] = []
        self._client = RemoteClient(plugin_id, self.base_url, timeout=timeout, transport=transport)

    def setup(self) -> None:
        manifest = self._client.fetch_manifest()
        if not PLUGIN_VERSION_PATTERN.match(manifest.version):
            raise RemoteToolFailedError(
                f"远程插件 {self.plugin_id} 的 version 必须是 MAJOR.MINOR.PATCH：{manifest.version!r}"
            )
        self.version = manifest.version
        self.description = manifest.description
        self._tools = [RemoteTool(spec, self._client) for spec in manifest.tools]

    def tools(self) -> list[Tool[Any, Any]]:
        return list(self._tools)


def _trace_headers() -> dict[str, str]:
    """把当前 trace 带给远端：远端照抄进自己的日志，就得出一条跨进程的链路。"""
    return outbound_trace_headers()


def _is_connect_failure(exc: httpx.HTTPError) -> bool:
    """请求是否**根本没到远端**（连接失败、连接超时、连接池超时）。

    只有这几种情况重试是安全的：写操作还没发出去，重试不会重复执行。
    """
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
