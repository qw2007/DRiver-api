# 阶段 6：远程插件（HTTP 契约 + 运行时注册与下线）

> 验收目标：远程插件注册后可调用，下线后工具自动不可用。

## 选型理由

- **HTTP + JSON，而不是 gRPC / 消息队列**：远端只要会说 HTTP 就能接（任意语言、任意框架），
  我们不必为每种语言发 SDK，也不必要求远端引入我们的运行时。代价是超时、重试、错误映射
  都要自己定，所以下面把这份契约钉成 1.0 版本。
- **拉模式（平台拉清单），而不是推模式（远端上报工具）**：装载的那一刻平台就知道工具的身份
  （`name` / `contract_version` / `input_schema`），拿不到清单就不装载、装载中发现冲突就整体回滚——
  失败发生在「注册」这一步，而不是第一次调用的时候。
- **不复刻 JSON Schema 校验器（不引 `jsonschema`）**：平台把远端的 `input_schema` 原样透出，
  参数由调用方（Agent）按远端 schema 生成，远端自己校验，平台只负责把远端的错误信封
  **原样透出**（含状态码映射）。两份校验器必然漂移，而「谁是参数的真话」只可能有一个答案：工具的
  提供方。
- **装载期同步拉清单、调用期异步转发**：`Plugin.setup()` 是阶段 2 定下的**同步**接口，且既在启动期
  （没有事件循环）也在运行时注册请求里被调用，所以清单必须用同步 `httpx.Client` 拉；工具调用发生在
  事件循环里，必须用 `httpx.AsyncClient`。注册端点是同步 `def`，FastAPI 会把它扔进线程池，
  阻塞式拉清单不会卡住事件循环。
- **每次调用新建 `AsyncClient`**：`Plugin.teardown()` 同样是同步接口，关不掉一个异步客户端；
  把收尾改成异步是破坏性变更（违反「插件契约版本化」）。代价是不复用连接池，记入已知限制。
- **开关默认关 + 独立管理作用域**：远程插件等于「运行时可注册的可执行入口」，默认关是八条原则里
  「特性开关默认关」的落地。管理接口与工具调用**分权**：`plugin_admin` 不是工具名，带它的钥匙
  调不了任何工具，反之只有工具作用域的钥匙也管理不了插件。
- **卸载接口只认远程插件**：本地插件的生命周期仍然只由配置（`DRIVER_PLUGINS`）决定，
  避免出现「用远程接口管本地插件」这种语义混乱——那样一份配置就有了两个真话。

## 模块设计

```
POST /v1/plugins/remote ──┐
                          ├─→ PluginHost.load/unload ─→ RemotePlugin ─→ RemoteClient ─→ 远端
DELETE /v1/plugins/remote/{id} ┘                        （工具注册进 ToolRegistry）
```

- `api/plugins.py`：三个端点。`GET ""`（阶段 2 起就有，按作用域过滤）不变；新增
  `POST /remote` 与 `DELETE /remote/{plugin_id}`。三道门依次是**开关** → **凭证** → **管理作用域**。
- `plugins/host.py`：复用阶段 2 的 `load()`（先 `setup()` 再逐个注册工具，失败整体 `_rollback`）
  与 `unload()`（**先摘账再 teardown**，所以「下线即不可用」不需要任何新机制）；本阶段只新增了
  `get(plugin_id)`，供卸载接口判断「这个 id 是不是远程插件」。
- `plugins/remote.py`：本阶段的新模块，七个成员：

  | 成员 | 作用 |
  | --- | --- |
  | `normalize_base_url(raw)` | 规整远端地址：去空白与尾斜杠，必须是带主机的 `http(s)://` |
  | `RemoteArguments` | 入参模型，`extra="allow"` 原样透传（平台不解释参数） |
  | `RemoteToolSpec` / `RemoteManifest` | 远端清单的契约模型 |
  | `RemoteClient` | 同步 `fetch_manifest()` + 异步 `call_tool()` + 错误映射 |
  | `RemoteTool` | 一个远端工具，`input_schema` 照抄远端清单，`output_model=None` 让结果透传 |
  | `RemotePlugin` | 身份 + 工具集合，装载期做四道自检 |

- **装载期的四道自检**（任何一条不过就 `RemoteToolFailedError`，宿主整体回滚）：清单里的
  `plugin_id` 必须与注册 id 一致；`version` 必须是 `MAJOR.MINOR.PATCH`（宿主装载前只见得到占位
  `0.0.0`）；`tools` 不能为空；每个工具的名字与契约版本还要过注册表的既有校验。
- **结果形状**：远端必须回 JSON **对象**，不是对象就报错——这样「成功」与「失败」在类型上就不会
  混。平台不做二次校验，也不改形状（`output_model=None`）。
- **错误映射**：远端信封里的 `code` **原样透出**，状态码按下表映射，这样调用方看到的词汇与我们
  自己抛的错误完全一致。

  | 远端 `code` | 平台状态码 |
  | --- | --- |
  | `invalid_request` / `invalid_arguments` | 422 |
  | `tool_not_found` | 404 |
  | `permission_denied` | 403 |
  | 其它（含未知 code） | 502 |

  连不上、超时、非 JSON、缺信封 → `remote_unavailable` 或 `remote_tool_failed`（都是 502，
  消息里**不含**远端地址，只给异常类型名，避免把内网拓扑写进对外错误）。

## 接口定义

### 环境变量

| 变量 | 默认 | 取值 | 语义 |
| --- | --- | --- | --- |
| `DRIVER_REMOTE_PLUGIN_ENABLED` | `0` | `0` / `1`（也接受 `true`/`false`/`yes`/`on`/`off`） | 打开管理接口；关闭时两个端点返回 404 `remote_plugin_disabled` |
| `DRIVER_REMOTE_PLUGINS` | 空 | `id=base_url[, id=base_url]` | 启动期装载的远程插件；地址非法/重复 id 启动即失败 |
| `DRIVER_REMOTE_TIMEOUT` | `5` | 正数（秒，有限值） | 拉清单与调用的单次超时；`0`/负数/`nan`/`inf`/非数字启动即失败 |

### 远端契约 1.0（远端要实现的两个端点）

```http
GET {base_url}/manifest
→ 200
{
  "plugin_id": "echo_farm",
  "version": "1.0.0",
  "description": "示例远程插件",
  "tools": [
    {"name": "remote_echo", "contract_version": "1.0", "description": "回声",
     "input_schema": {"type": "object", "properties": {"message": {"type": "string"}},
                      "required": ["message"], "additionalProperties": false}}
  ]
}

POST {base_url}/execute
{"tool": "remote_echo", "arguments": {"message": "hi", "times": 3}}
→ 200 {"tool": "remote_echo", "contract_version": "1.0",
       "result": {"echo": "hi", "times": 3, "repeated": ["hi", "hi", "hi"]}}
→ 失败时用平台的信封：{"error": {"code": "invalid_arguments", "message": "……"}}
```

`result` 必须是 JSON 对象；`input_schema` 是调用方唯一能看到参数要求的地方，平台原样照抄。

### `POST /v1/plugins/remote`

```http
POST /v1/plugins/remote
X-API-Key: admin-key
{"plugin_id": "echo_farm", "base_url": "http://127.0.0.1:9100/"}

→ 201
{"plugin_id": "echo_farm", "version": "1.0.0", "description": "示例远程插件",
 "tools": [{"name": "remote_echo", "contract_version": "1.0"}]}
```

请求体 `extra="forbid"`（写错字段名 422，而不是被静默忽略）。工具身份在注册响应里就足够了，
完整 `input_schema` 去 `GET /v1/tools` 看——注册响应只是一张收货单。

三道门与对应错误：开关关 → 404 `remote_plugin_disabled`；`DRIVER_AUTH_MODE=required` 时无凭证 →
401 `missing_credentials`；有凭证但没有 `plugin_admin`（且不是全权钥匙）→ 403 `permission_denied`，
details 里写明 `需要作用域：plugin_admin`。重复注册同一 id → 500 `plugin_load_failed`；
工具撞名（如远端也叫 `echo`）→ 同样 500，且**整批回滚**，不会留下半个插件。

### `DELETE /v1/plugins/remote/{plugin_id}`

```http
DELETE /v1/plugins/remote/echo_farm   → 204（无响应体）
```

同样的三道门。id 不存在 → 404 `plugin_not_found`；id 是本地插件 → 404 `plugin_not_found`
（消息里说明「本接口只管理远程插件」）。下线后 `/v1/tools` 里立刻没有它，调用返回
404 `tool_not_found`——复用既有的卸载语义，没有为「下线」发明新状态。

## 关键代码

```python
# plugins/remote.py —— 调用期：异步转发 + 错误映射（远端 code 原样透出）
async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    payload = {"tool": tool, "arguments": dict(arguments)}
    async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
        response = await client.post(f"{self._base_url}{EXECUTE_PATH}", json=payload)
    body = self._decoded(response)          # 非 JSON → remote_tool_failed
    if response.status_code != 200:
        raise self._remote_error(response.status_code, body)
    result = body.get("result")
    if not isinstance(result, dict):
        raise RemoteToolFailedError("远端返回的 result 不是 JSON 对象")
    return result
```

```python
# plugins/remote.py —— 工具的 schema 来自远端，不做二次加工
def manifest(self) -> ToolManifest:  # type: ignore[override]
    return ToolManifest(
        name=self.name,
        contract_version=self.contract_version,
        description=self.description,
        input_schema=dict(self._spec.input_schema),   # 原样照抄
        output_schema=None,                            # 远端不承诺结果形状
    )
```

```python
# api/plugins.py —— 三道门：开关 → 凭证 → 管理作用域
def _require_remote_api(settings: Settings) -> None:
    if not settings.remote_plugin_enabled:
        raise RemotePluginDisabledError(
            "远程插件管理接口未开启：设 DRIVER_REMOTE_PLUGIN_ENABLED=1 后重启"
            "（默认关，理由见 docs/stages/stage-6.md）"
        )

def _require_plugin_admin(principal: Principal) -> None:
    if not principal.can_manage_plugins:
        raise PermissionDeniedError(
            f"该凭证没有插件管理作用域 {PLUGIN_ADMIN_SCOPE}",
            details=[{"type": "permission_denied", "loc": ["scope"],
                      "msg": f"需要作用域：{PLUGIN_ADMIN_SCOPE}"}],
        )
```

```python
# bootstrap.py —— 启动期装载：连不上就起不来（刻意，见「已知限制」）
for plugin_id, base_url in settings.remote_plugins:
    host.load(RemotePlugin(plugin_id, base_url, timeout=settings.remote_timeout))
```

## 测试与部署方案

- **测试**：新增 `tests/test_remote_plugin.py`（24 个用例，全部用 `httpx.MockTransport` 注入，
  零网络）、`tests/test_remote_api.py`（20 个用例，走 `TestClient` + conftest 里一个**真 HTTP**
  的远端，覆盖启动期装载路径）；`tests/test_config.py` 增 7 组、`tests/test_protocol_isolation.py`
  增一条守卫（全仓库只有 `plugins/remote.py` 可以 import `httpx`）。
- **本机两步验收**（下方证据即来自这两条命令）：

  ```bash
  # 1) 起一个示例远端（仓库自带）
  .venv\Scripts\python.exe examples\remote_echo_service.py --port 9100

  # 2) 起平台（开关开 + 强制凭证 + 一把管理钥匙）
  $env:DRIVER_REMOTE_PLUGIN_ENABLED='1'
  $env:DRIVER_AUTH_MODE='required'
  $env:DRIVER_API_KEYS='admin-key:plugin_admin,all-key'
  .venv\Scripts\python.exe -m uvicorn driver_platform.main:app --host 0.0.0.0 --port 8000
  ```

- **部署**：阶段 7 的 compose 会把 `base_url` 从 `host.docker.internal:9100` 换成服务名；
  生产上远端是**跨信任边界**的调用，需要出网白名单、TLS 与「远端清单变更后重新注册」的流程，
  本阶段只保证契约与失败路径清楚。

## 非目标（本阶段明确不做）

- 插件包的上传、存储与执行（本阶段只连**已经在跑**的远端服务）。
- 清单与来源的签名校验、mTLS、出网白名单。
- 重试、熔断、退避、清单缓存与热更新（远端抖动在本阶段直接表现为 502）。
- 远端多副本的负载均衡与故障转移（`base_url` 是单点）。
- 本地插件的 HTTP 管理接口（仍然只由配置决定）。
- 远端调用的流式/长任务协议。

## 已知限制

1. **每次调用新建 `httpx.AsyncClient`**：不复用连接池、不保持 keep-alive，省下的复杂度是
   「不必把插件契约改成异步收尾」。高频调用时应先用压测确认这笔开销可接受。
2. **清单只在装载时拉一次**：远端改了 `input_schema` 或加了工具，平台不会知道，要重新注册
   （`DELETE` 后再 `POST`）。
3. **没有重试与熔断**：远端超时或 5xx 直接变成调用方的 502，重试策略留给调用方。
4. **远端是信任边界之外的可执行入口**：没有签名与来源校验，谁能注册、`base_url` 能指向哪里，
   完全由「开关 + 管理作用域 + 网络可达性」决定。
5. **平台照抄远端 `input_schema`**：远端把 schema 写错，Agent 就会按错的生成参数——平台不背这个
   锅，但也没有替远端兜底的能力（这是「单一真话」的代价）。
6. **超时是单次请求超时**：不是整段业务超时，长任务需要远端自己分片。
7. **启动期装载失败 = 进程起不来**：`DRIVER_REMOTE_PLUGINS` 里的远端连不上，`create_app()` 直接
   抛错。这是刻意的（宁可起不来，也不要带着一半坏工具对外服务），但它意味着**运行时的强依赖**
   必须写进部署编排的启动顺序里。
8. **下线不通知远端**：远端不知道平台已经不再调用它。

## 验收证据

### 1) 门禁

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `ruff check .` | All checks passed |
| 格式 | `ruff format --check src tests` | 54 files already formatted |
| 类型 | `mypy src` | Success: no issues found in 34 source files |
| 测试 | `pytest -q -p no:cacheprovider --cov=driver_platform` | **241 passed**（0 failed / 0 error），覆盖率 **95%**（`TOTAL 1248 68`） |

`plugins/remote.py` 自身覆盖率 97%（121 语句 4 未覆盖），`api/plugins.py` 98%，`plugins/host.py` 96%。

### 2) 真实 HTTP 全程（示例远端 9100 + 平台 8000，逐条为实际响应）

| # | 请求 | 结果 |
| --- | --- | --- |
| 1 | `POST /v1/plugins/remote`（`tool-key`，只有 `echo` 作用域） | **403** `permission_denied`，details：`需要作用域：plugin_admin` |
| 2 | 同上（`admin-key`，`plugin_admin`），`base_url` 带尾斜杠 | **201**，`{"plugin_id":"echo_farm","version":"1.0.0","tools":[{"name":"remote_echo","contract_version":"1.0"}]}` |
| 3 | `GET /v1/tools`（全权钥匙） | **200**，`remote_echo` 的 `input_schema` 与远端清单逐字一致（`times` 的 `minimum:1`/`maximum:10` 都在） |
| 4 | `POST /v1/tools/execute {"tool":"remote_echo","arguments":{"message":"hi","times":3}}` | **200** `{"tool":"remote_echo","contract_version":"1.0","result":{"echo":"hi","times":3,"repeated":["hi","hi","hi"]}}` |
| 5 | 同上，`times=99`（远端自己校验） | **422** `invalid_arguments`，消息为远端原文 |
| 6 | `DELETE /v1/plugins/remote/echo_farm` | **204** |
| 7 | 再 `GET /v1/tools` / 再调用 | 清单里**没有** `remote_echo`；调用 **404** `tool_not_found`（下线即不可用） |
| 8 | 开关关（默认）时 `POST /v1/plugins/remote` | **404** `remote_plugin_disabled`，消息指明要设 `DRIVER_REMOTE_PLUGIN_ENABLED=1` |

### 3) 与阶段 5 的接缝（远程调用照样可观测）

远端工具调用和本地工具走同一条执行管线，所以指标与日志自动覆盖：

```
# 带调用方指定的 X-Request-ID: stage6-demo.1
{"ts": "2026-10-01T07:18:52.126Z", "level": "INFO", "logger": "driver_platform.tools",
 "msg": "tool_call", "request_id": "stage6-demo.1", "event": "tool_call",
 "tool": "remote_echo", "outcome": "success", "duration_ms": 11.075}

# /metrics 里对应的样本（远程工具与本地工具同名同列）
driver_tool_calls_total{outcome="success",tool="remote_echo"} 1.0
```

（`event=tool_call` 的日志行与指标同源于 `track_tool_call()`，因此**只在
`DRIVER_METRICS_ENABLED=1` 时出现**；`http_request` 访问日志始终有。）

### 4) 真实缺陷（本阶段自己踩出来并修掉的）

- **环状导入**：`config → plugins.remote → plugins/__init__ → catalog → config`，跑到测试才炸
  `ImportError: cannot import name 'Settings' from partially initialized module`。修法是把
  `catalog.py` 里的 `Settings` 挪进 `if TYPE_CHECKING:`（与 `security/` 两个模块同一套做法）。
- **`normalize_base_url` 漏了主机校验**：`http://`、`http:///path`、`//remote.test` 原本都能过，
  现在用 `rest.split("/", 1)[0]` 判空后拒绝。
- **只校验了占位版本**：宿主装载前看到的是 `0.0.0`，远端清单里的 `version` 没人校验；
  `RemotePlugin.setup()` 现在要求 `MAJOR.MINOR.PATCH`。
