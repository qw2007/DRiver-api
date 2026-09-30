# 阶段 1：核心服务 + 工具注册

> 目标：`execute` 统一入口；`echo` 工具；非法参数 → 422。状态：已完成（本阶段）。

## 选型理由

**为什么是「一个统一入口」而不是 `/v1/tools/{name}/execute`**：调用方是 AI Agent，工具名在它那里是**数据**（来自工具列表），不是路径的一部分。请求体 `{tool, arguments}` 与 MCP 的 `tools/call`（`{name, arguments}`）同构，阶段 3 的适配层只是字段改名，不用改路由；同时 `POST /v1/tools/execute` 只有一个路由，鉴权（阶段 4）、限流、指标（阶段 5）只需要挂一次。

**为什么先定错误契约再写功能**：Agent 的重试/降级逻辑只可能按 `error.code` 分支，不能解析自然语言。所以任何失败——包括 FastAPI 自己的请求体校验失败——都必须落进同一个信封 `{"error": {code, message, details}}`，否则调用方要写两套分支。这是「接口先行」在本阶段最具体的一次落地。

**为什么用 Pydantic 模型当工具契约**：`input_model` 一物三用——运行时校验（422 的来源）、`model_json_schema()` 生成对外 JSON Schema（阶段 3 的 OpenAI / MCP 工具清单直接吃它）、序列化输出。手写 schema 会在第三处漂移。

**为什么注册表要显式报错而不是覆盖**：`DuplicateToolError` / `InvalidToolError` 都发生在启动期，工具名写错、契约版本号格式不对应该让进程起不来，而不是等到线上调用才发现。

## 模块设计

```
src/driver_platform/
  main.py              create_app(registry=None)：装配注册表、错误处理、路由
  errors.py            PlatformError 体系 + 统一错误信封 + FastAPI 异常处理注册
  api/tools.py         GET /v1/tools、POST /v1/tools/execute
  tools/base.py        Tool 基类（契约）+ ToolManifest（对外说明书）
  tools/registry.py    ToolRegistry：注册、查找、列出（唯一真相）
  tools/echo.py        EchoTool：示例工具，同时是 422 的载体
```

职责边界：**路由只做编排**（查工具 → 校验入参 → 执行 → 包装输出），**注册表只管存取**（不关心工具从哪来，阶段 2 的插件、阶段 6 的远程注册都往这里写），**错误只在一处定义**（`errors.py`）。注册表通过 `app.state.registry` 注入，`create_app(registry=...)` 可整体替换——测试和未来的插件宿主走同一个口子，不改路由。

## 接口定义

### `GET /v1/tools`

```json
200 [{"name": "echo", "contract_version": "1.0", "description": "...",
      "input_schema": {"type": "object", "properties": {"message": {"type": "string"},
                                                       "times": {"type": "integer", "minimum": 1, "maximum": 5}},
                       "required": ["message"]},
      "output_schema": {...}}]
```

按工具名排序，行为确定。阶段 3 的 MCP `tools/list` 与 OpenAI function 清单都是这份结构的投影。

### `POST /v1/tools/execute`

请求：`{"tool": "echo", "arguments": {"message": "hi", "times": 2}}`

成功（200）：`{"tool": "echo", "contract_version": "1.0", "result": {"echo": "hi\nhi", "times": 2}}`

失败：

| HTTP | `error.code` | 触发条件 |
|---|---|---|
| 404 | `tool_not_found` | 工具名没注册 |
| 422 | `invalid_arguments` | 工具存在，`arguments` 不满足 `input_schema` |
| 422 | `invalid_request` | 请求体本身不合法（缺 `tool`、`arguments` 不是对象） |
| 500 | `tool_execution_failed` | 工具内部抛异常 |
| 500 | `invalid_tool` / `duplicate_tool` | 插件注册期错误（正常在启动期就终止） |

`details` 是 pydantic 校验错误的裁剪版（只留 `type`/`loc`/`msg`，剥掉可能含异常对象或回显用户原文的 `ctx`/`input`/`url`）。

## 契约与版本化规则

- 工具名：`^[a-z][a-z0-9_]{0,63}$`；`contract_version`：`MAJOR.MINOR`。
- 破坏性变更（改字段、改 code、删字段）→ 升 MAJOR；只加可选字段 → 升 MINOR。平台不替插件做兼容层，调用方按 MAJOR 分支。
- `execute` 拿到的永远是**已校验**的入参对象；返回后由平台按 `output_model` 校验并序列化，所以「声明的 output_schema」和「实际返回」不会漂移。

## 关键代码

```python
class Tool(ABC, Generic[InT, OutT]):
    name: str
    contract_version: str = "1.0"
    description: str = ""
    input_model: type[InT]
    output_model: type[OutT] | None = None

    @classmethod
    def manifest(cls) -> ToolManifest: ...

    @abstractmethod
    async def execute(self, arguments: InT) -> OutT: ...
```

```python
@router.post("/execute", response_model=ExecuteResponse)
async def execute_tool(request: Request, body: ExecuteRequest) -> ExecuteResponse:
    tool = _registry(request).get(body.tool)          # 未知工具 → 404
    try:
        arguments = tool.input_model.model_validate(body.arguments)
    except ValidationError as exc:
        raise InvalidArgumentsError(..., details=sanitize_validation_errors(exc.errors())) from exc
    try:
        result = await tool.execute(arguments)
    except PlatformError:
        raise
    except Exception as exc:
        raise ToolExecutionError(f"工具 {tool.name} 执行失败") from exc
    ...
```

## 测试与部署方案

测试（17 条，`pytest` 0.06s）：注册表单测覆盖重复注册、未知工具、非法契约、manifest schema；接口测试覆盖列表结构、成功形状、`times` 边界（99 → 422）、缺必填、请求体缺 `tool`、`arguments` 非对象、工具抛错 → 500 信封。

手工冒烟：

```bash
.venv/Scripts/python -m uvicorn driver_platform.main:app --port 8000
curl http://127.0.0.1:8000/v1/tools
curl -X POST http://127.0.0.1:8000/v1/tools/execute -H "Content-Type: application/json" \
     -d '{"tool":"echo","arguments":{"message":"hi","times":2}}'
curl -i -X POST http://127.0.0.1:8000/v1/tools/execute -H "Content-Type: application/json" \
     -d '{"tool":"echo","arguments":{"message":"hi","times":99}}'   # 期望 422 invalid_arguments
```

CI 不变：`ruff check .` + `pytest`（Python 3.14）。

## 非目标（留给后续阶段）

- 鉴权、限流、request_id、指标（阶段 4/5）
- 插件加载与数据库工具（阶段 2）、协议适配（阶段 3）
- 配置外部化：目前没有可配项，等出现第一个真实配置（超时、开关）时再引 `pydantic-settings`，不提前加依赖
- 工具的同步/流式返回、并发限流、超时取消

## 已知限制

- 工具超时与并发控制未做，长耗时工具会占住请求。
- `execute` 的异常细节只进服务端日志（阶段 5 才有结构化日志），当前只保留稳定 code 与固定 message。
