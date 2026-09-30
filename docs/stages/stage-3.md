# 阶段 3：协议适配层（MCP + OpenAI Schema）

> 目标：MCP 客户端能 list/call `search_database`；curl 返回 OpenAI 工具 JSON Schema。状态：已完成。

## 选型理由

**为什么用官方 `mcp` SDK，而不是自己手写 JSON-RPC**：MCP 是**有版本的对外协议**（`initialize` 协商协议版本、capabilities、`protocolVersion` 演进）。手写一份「看起来像 MCP」的 JSON-RPC，最可能的结局是：对自家测试全绿、对真实客户端（Claude Desktop / Codex / Claude Code）半通。协议适配层的价值恰恰是**别人能接上**，所以协议细节交给官方 SDK，我们只写「注册表 ↔ MCP 类型」的翻译。

**为什么把 mcp 放可选依赖**：官方 SDK 会拖进 `cryptography` / `pywin32` / `jsonschema` / `pydantic-settings` / `sse-starlette` 一整套。核心服务（HTTP + 工具 + 插件）不需要这些，凭「只跑 HTTP」的部署不该被迫背这个重量。因此 `pip install driver-platform` 不带 MCP，要用的装 `driver-platform[mcp]`。**代价**：MCP 入口多一个「依赖没装」的失败模式，用一句人话报错兜住（`python -m driver_platform.protocols.mcp_server` 会打印 `pip install "driver-platform[mcp]"` 并以退出码 2 结束，不给 traceback）。

**为什么 MCP 只做 stdio，不做 HTTP/SSE**：stdio 是本地 Agent（Claude Desktop、Codex）最普遍、也最安全的接法——进程由 Agent 自己 spawn，不存在「谁都能连」的监听端口。远程传输要处理会话、鉴权与并发，那正是阶段 4（安全权限）与阶段 6（远程插件）的题目；现在做一个半成品 HTTP 端点，只会让阶段 4 推倒重来。**因此本阶段 HTTP 侧没有 MCP 端点**，不为对称好看造接口。

**为什么 OpenAI 那边只导出 Schema、不装 openai SDK**：`GET /v1/protocols/openai/tools` 是**纯翻译**（平台 JSON Schema → Chat Completions 的 `tools[]`），不调用模型。依赖 openai SDK 会把「导出工具定义」和「调用模型」绑在一起，而后者是 Agent 自己的职责。

**为什么协议也是「默认关 + 写错就起不来」**：工具的对外暴露面属于安全边界。`DRIVER_PROTOCOLS` 默认空（HTTP 侧 404 `protocol_not_enabled`）；`DRIVER_PROTOCOLS=langchain` 这种不支持的协议名在 `Settings.from_env()` 就抛 `unknown_protocol`，绝不免打扰地忽略。

**为什么两家的错误映射不一样**：MCP 的约定是「工具执行失败」属于**结果**（`CallToolResult.isError = true`，模型能看见失败原因并自我纠正），只有协议层问题（未知方法、坏 JSON-RPC）才是 `-32xxx` 错误；而 HTTP 侧沿用阶段 1 的稳定错误信封。两边都保留「平台错误码」这个不变量，Agent 无论走哪个协议都能按 `code` 分支。

**为什么把执行路径抽成 `tools/executor.py`**：MCP 适配器要复用「取工具 → 校验入参 → 执行 → 序列化输出」这条链路。若直接在适配器里再写一遍，两份校验逻辑迟早分叉——HTTP 报 `invalid_arguments`、MCP 报别的，Agent 就得按协议分支写代码。所以阶段 3 把这段从 `api/tools.py` 提到 `tools/executor.py`，两个入口共用一条路径（HTTP 侧只是把 `ExecutionResult` 套进 `ExecuteResponse`）。

## 模块设计

```
src/driver_platform/
  config.py                   + DRIVER_PROTOCOLS（默认空）+ PROTOCOL_NAMES
  tools/executor.py           execute_tool()：HTTP 与 MCP 共用的执行路径
  protocols/__init__.py       require_protocol(settings, name) + build_router()（装配出口）
  protocols/openai_schema.py  ToolManifest → OpenAI tools[]（纯函数，零协议依赖）
  protocols/mcp.py            注册表 → MCP Server（**唯一** import mcp SDK 的适配器）
  protocols/mcp_server.py     stdio 进程入口：python -m driver_platform.protocols.mcp_server
  protocols/routes.py         GET /v1/protocols/openai/tools
```

职责边界：适配器**只读**注册表，不改工具、不碰插件宿主、不装配自己（装配一律走 `bootstrap.create_registry_and_host()`，HTTP 与 stdio 两个入口共用同一条装配路径）。反过来，核心服务不知道适配器存在——`tests/test_protocol_isolation.py` 用 AST 扫 import 把这条边界钉成测试：只有 `protocols/mcp.py` 与 `protocols/mcp_server.py` 可以 import `mcp`（且前者必须在名单里，防止「适配器写成空壳也过测」）。核心只依赖 `driver_platform.protocols` 这个包的公共面（`require_protocol` / `build_router`），不 import 任何具体适配器模块。

## 接口定义

### MCP（stdio）

客户端 spawn `python -m driver_platform.protocols.mcp_server`，走标准 MCP 流程：`initialize` → `tools/list` → `tools/call`。**stdout 是协议通道**，本模块与它调用的代码只把诊断写 stderr。

`tools/list` 每一项（`echo` 节选）：

```json
{"name": "echo", "title": "echo", "description": "把输入原样回显……",
 "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}, "times": {"type": "integer"}}, "required": ["message"]},
 "outputSchema": {"type": "object", "properties": {"echo": {"type": "string"}, "times": {"type": "integer"}}, "required": ["echo", "times"]}}
```

`inputSchema` / `outputSchema` 直接取平台 manifest 的同名 JSON Schema（不再手写一份），所以 MCP 看到的契约与 `GET /v1/tools` 逐字段一致。

`tools/call` 成功：

```json
{"content": [{"type": "text", "text": "{\n  \"echo\": \"hi\\nhi\",\n  \"times\": 2\n}"}],
 "structuredContent": {"echo": "hi\nhi", "times": 2},
 "isError": false}
```

`tools/call` 失败（工具级 —— 注意 `isError` 为真，而不是 JSON-RPC 错误）：

```json
{"content": [{"type": "text", "text": "{\n  \"error\": {\n    \"code\": \"tool_not_found\", …\n  }\n}"}],
 "structuredContent": {"error": {"code": "tool_not_found", "message": "未注册的工具：ghost", "details": null}},
 "isError": true}
```

平台错误在两种传输上的映射（同一个 `code`，两种表达）：

| 平台情况 | HTTP | MCP（stdio） |
|---|---|---|
| `tool_not_found` 未知工具 | 404 信封 | 结果 `isError=true`，`structuredContent.error.code` |
| `invalid_arguments` 参数非法 | 422 信封 | 同上 |
| `tool_execution_failed` 工具内部失败 | 500 信封 | 同上 |
| `protocol_not_enabled` 协议没开 | `/v1/protocols/*` → 404 | 入口进程退出码 2，stderr 打印同一信封 |
| `unknown_protocol` 协议名不支持 | 启动期即失败 | 启动期即失败 |

### `GET /v1/protocols/openai/tools`

```json
200 {"tools": [{"type": "function",
                "function": {"name": "echo", "description": "把输入原样回显……",
                             "parameters": {"type": "object", "properties": {…}}}}]}
```

- `parameters` 就是平台 `input_schema`：Chat Completions 的 `function.parameters` 也是 JSON Schema，无需改写，也不额外发明字段。
- 只读：不改工具、不改注册表；工具装了插件就是几个，导出就是几个。
- 没开 `openai` → 404 `protocol_not_enabled`（信封与阶段 1 一致）。
- **不调用模型**：这里只导出定义，`tool_calls` 回填与结果回灌是 Agent 自己的事。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DRIVER_PROTOCOLS` | 空（全关） | 逗号分隔，可选 `mcp` / `openai`；含未知名 → 启动失败 `unknown_protocol` |

## 关键代码

```python
# protocols/mcp.py：注册表 → MCP Server
@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [to_mcp_tool(manifest) for manifest in registry.manifests()]


@server.call_tool(validate_input=False)   # 校验交给平台，保住自己的错误码
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    try:
        outcome = await execute_tool(registry, name, arguments)
    except PlatformError as exc:
        return error_result(exc)          # isError=true：模型能看见失败原因
    return success_result(outcome.result)
```

```python
def to_mcp_tool(manifest: ToolManifest) -> types.Tool:
    return types.Tool(
        name=manifest.name,
        title=manifest.name,              # 客户端列表里的短名
        description=manifest.description or None,
        inputSchema=manifest.input_schema,    # 同一份契约，不改写
        outputSchema=manifest.output_schema,
    )


def error_result(exc: PlatformError) -> types.CallToolResult:
    payload = exc.to_payload()            # 与 HTTP 同一个信封函数
    return types.CallToolResult(content=[_text(payload)], structuredContent=payload, isError=True)
```

```python
# protocols/openai_schema.py：纯翻译，字段一一对应
def to_openai_tool(manifest: ToolManifest) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": manifest.name,
            "description": manifest.description,
            "parameters": manifest.input_schema,
        },
    }
```

## 测试与部署方案

- **翻译层单测**（`tests/test_mcp_adapter.py`）：`ToolManifest` ↔ MCP `Tool` 逐字段一致；成功结果带 `structuredContent` 与 JSON 文本；没有 `output_model` 的工具不硬造结构化对象（`structuredContent=None`）；错误结果与 HTTP 信封同构。
- **端到端**（`tests/test_mcp_server.py`）：用**官方** `mcp` 客户端 spawn 真实进程（`stdio_client` + `ClientSession`）——不是自造假客户端，因为假客户端只能证明「我实现的符合我以为的协议」。断言：`initialize` 的 `serverInfo.name`、声明了 tools 能力、`tools/list` 含 `echo` 与 `search_database` 且 `search_database.inputSchema.properties` 为 `table/columns/filters/limit/offset`、`outputSchema` 非空、`echo` 调用拿到结构化结果、`search_database` 拿到 `row_count/total/truncated`、未知工具与缺参数 → `isError=true` + 平台错误码；未开开关时子进程退出码 2；没装可选依赖时打印安装提示且不吐 traceback（用假 `mcp` 包模拟）。
- **开关与隔离**（`tests/test_config.py`、`tests/test_protocol_isolation.py`）：协议默认全关、`" openai , mcp ,, "` 解析、非法协议名启动失败；AST 扫 import 证明只有 MCP 适配器依赖 SDK、OpenAI 适配器零协议依赖、核心不 import 具体适配器。
- **OpenAI 导出**（`tests/test_openai_schema.py`）：开关关闭 → 404；导出与注册表契约逐字段一致；装了数据库插件后导出随工具列表变化。
- 手工冒烟（HTTP 侧）：

```bash
DRIVER_PROTOCOLS=openai .venv/Scripts/python -m uvicorn driver_platform.main:app --port 8000
curl http://127.0.0.1:8000/v1/protocols/openai/tools
```

- 接到真实 Agent（Claude Desktop / Codex 的 `mcpServers`）：

```json
{"mcpServers": {"driver-platform": {
  "command": "D:\\DSh\\driver-platform-v2\\.venv\\Scripts\\python.exe",
  "args": ["-m", "driver_platform.protocols.mcp_server"],
  "env": {"DRIVER_PROTOCOLS": "mcp", "DRIVER_PLUGINS": "sqlite", "DRIVER_SQLITE_PATH": "/tmp/demo.db"}}}}
```

CI 不用改：`dev` extra 里已含 `mcp`，`ruff check` / `ruff format --check src tests` / `mypy` / `pytest` 四步照跑。

## 非目标（留给后续阶段）

- MCP over HTTP/SSE、远程 MCP 客户端接入（阶段 4 鉴权 + 阶段 6 远程插件）
- MCP 的 `resources` / `prompts` / `sampling` / 进度通知：本阶段只做 tools
- 鉴权与权限（阶段 4）、request_id 与指标（阶段 5）
- OpenAI 侧的模型调用、流式响应、`tool_calls` 回填：平台只导出定义
- 多客户端并发：stdio 一次服务一个客户端进程

## 已知限制

- **协议版本跟随 SDK**：`initialize` 回什么版本由 SDK 决定（本机 `mcp` 1.30.0 为 `2025-11-25`），平台不写死；升级 SDK 可能改变协商结果，需要重跑端到端测试。
- **入参只认结构化 `arguments`**：MCP 的 `tools/call` 必须给对象参数；其他形状会落到平台校验上，报 `invalid_arguments`（`isError=true`）。
- **`structuredContent` 的形状必须匹配 `outputSchema`**：官方客户端在 `isError=false` 时按 `outputSchema` 校验（`isError=true` 时跳过），所以工具的 `output_model` 就是协议层的输出契约；改它属破坏性变更，按原则要升 `contract_version` 主版本。
- **stdio 依赖进程管道**：客户端不关管道就退出（被 kill）时，`host.close()` 可能来不及跑——SQLite 是只读连接，影响可忽略；有写状态的插件要等阶段 6 的托管方式。
- **本机沙箱跑测试与冒烟要提权**：受限模式下 `0o700` 临时目录不可访问（`tmp_path` 建不出 SQLite 库），子进程的匿名管道也不可创建（`stdio_client` spawn 服务端报 `PermissionError: [WinError 5]`）。CI（ubuntu）不受影响。

## 验收证据

- `ruff check .` → `All checks passed!`
- `ruff format --check src tests` → `34 files already formatted`
- `mypy` → `Success: no issues found in 23 source files`
- `pytest` → `74 passed`（含官方 MCP 客户端 spawn 真进程的端到端用例）
- 手工 MCP 冒烟（官方客户端 + 真子进程 + 临时 SQLite 库）：`serverInfo = driver-platform 0.0.0`、`protocolVersion = 2025-11-25`、`tools = [echo, search_database]`、`search_database{status:active,limit:2}` → `rows 2 / total 3 / truncated true`、未知表 → `isError=true` + `invalid_arguments`/`unknown_table`（中文信息原样往返）
- 真实 HTTP 冒烟（`DRIVER_PROTOCOLS=openai` + 临时 SQLite 库，curl）：`GET /v1/protocols/openai/tools` → `HTTP=200`，`tools = [echo, search_database]`、`type = function`、`search_database.function.parameters.properties = [table, columns, filters, limit, offset]`、`required = [table]`、无多余字段；未开协议时 404 `protocol_not_enabled`（测试覆盖）
