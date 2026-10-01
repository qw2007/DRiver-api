# 变更日志（CHANGELOG）

驱动平台自阶段 0 起的完整实现日志。每条对应一个提交，记录**目标 / 交付物 / 关键决策与理由 / 验收证据**。

- 整体任务目标见 [`docs/task-brief.md`](docs/task-brief.md)；
- 每阶段的设计说明（技术选型理由 / 模块设计 / 接口定义 / 关键代码 / 测试与部署方案）见 [`docs/stages/`](docs/stages/)。

## 概览

| 阶段 | 提交 | 时间 | 变更量 | 对外验收 |
|---|---|---|---|---|
| 0 骨架 + CI | `9cea789` | 2026-09-30 14:36 | 9 files, +166 | `GET /health` → `{"status":"ok"}`；CI 跑 ruff + pytest |
| 1 核心服务 + 工具注册 | `99c9b1e` | 2026-09-30 14:38 | 14 files, +742 / −18 | `POST /v1/tools/execute` 统一入口；echo；非法参数 422 |
| 2 插件系统 + 数据库插件 | `5efc1c0` | 2026-09-30 20:36 | 23 files, +1296 / −36 | 工具表出现 `search_database`；插件可装载/卸载；结构化结果 |
| 3 协议适配层 | `6ee7632` | 2026-09-30 20:49 | 21 files, +1018 / −47 | MCP 客户端 list/call `search_database`；OpenAI Schema 导出 |
| 4 安全权限 | `e9750c1` | 2026-09-30 20:57 | 18 files, +912 / −33 | 无凭证 401；越权 403 |

---

## 阶段 0：骨架 + CI（`9cea789`，2026-09-30）

**目标**：可运行的最小 FastAPI 应用 + 质量门禁 + CI。

**交付物**：`pyproject.toml`（hatchling，`requires-python >=3.11`）、`src/driver_platform/{__init__,main}.py`、`tests/test_health.py`、`.github/workflows/ci.yml`、`Dockerfile`、`README.md`、`.gitignore`、`.gitattributes`。

**关键决策与理由**

- 用 `create_app()` 工厂而不是模块级裸应用：测试与嵌入方可以自己装配依赖（后面阶段加插件、加配置都靠这个口子）；
- CI 直接钉 **Python 3.14**：本机就是 3.14，先保证跑得通的那个版本绿，`requires-python` 仍放宽到 3.11+；
- `Dockerfile` 先写好但**明确不验证**：本机没有 Docker，留到阶段 7 一起验。

**验收证据**：`GET /health` 真实 uvicorn 返回 `HTTP 200` + `{"status":"ok"}`；`ruff check`、`pytest`（1 passed）全绿。

**已知限制**：mypy 尚未接入（契约未定，接了也是白接）；CI 只测 3.14。

## 阶段 1：核心服务 + 工具注册（`99c9b1e`，2026-09-30）

**目标**：统一执行入口 + 工具契约 + 稳定错误码。

**交付物**：`errors.py`（错误信封 + `code` 体系 + FastAPI 校验错误接管）、`tools/{base,registry,echo}.py`、`api/tools.py`、`main.py` 装配、测试（`tests/test_registry.py`、`tests/test_tools.py`）。

**关键决策与理由**

- **统一入口 `POST /v1/tools/execute` 收 `{tool, arguments}`，而不是 `/v1/tools/{name}/execute`**：在 Agent 眼里工具名是**数据**，不是 URL 结构；`{tool, arguments}` 与 MCP 的 `tools/call {name, arguments}` 同构，阶段 3 只需要改名映射；
- **先定错误契约再写功能**：否则调用方要同时处理 FastAPI 默认的 `{"detail":...}` 和业务错误两套分支。统一为 `{"error":{"code","message","details"}}`，`code` 是稳定标识、属于对外契约；
- **用 pydantic `input_model` 当插件契约**：一物三用 —— 运行时校验（422 的来源）、生成对外 JSON Schema、序列化输出；
- 注册表在启动期显式报错（`invalid_tool` / `duplicate_tool`），**不静默覆盖**。

**验收证据**：`/v1/tools` 返回契约清单；`echo{message:"hi",times:2}` → `{"tool":"echo","contract_version":"1.0","result":{"echo":"hi\nhi","times":2}}`；缺字段 → 422 `invalid_request`；`times=99` → 422 `invalid_arguments`（`details` 只留 `type`/`loc`/`msg`）；pytest 17 passed。

**已知限制**：工具执行无超时、无并发限制。

## 阶段 2：插件系统 + 数据库插件（`5efc1c0`，2026-09-30）

**目标**：插件宿主 + 第一个真实工具 `search_database`。

**交付物**：`config.py`（`Settings.from_env()`）、`plugins/{base,host,catalog,sqlite}.py`、`api/plugins.py`、`bootstrap.py`、`tools/registry.py`（新增 `unregister`）、`docs/stages/stage-2.md`；CI 增加 mypy。

**关键决策与理由**

- **插件是有身份（`plugin_id`）、有版本、可装载/卸载的单元，工具只是它的产物**：阶段 6 的远程插件只需要换掉 `Plugin` 的实现，宿主契约不动；
- **`search_database` 收结构化参数 `{table, columns, filters, limit, offset}`，不让 Agent 写 SQL**：中间层的价值是**约束** —— 表名/列名必须命中 introspect 出来的真实 schema，值一律用 `?` 占位符绑定（SQL 注入面收敛到标识符白名单 + 双引号转义），返回 `row_count`/`total`/`truncated` 让分页语义确定；
- **插件默认不启用**（`DRIVER_PLUGINS` 默认空）：启用了 `sqlite` 却没给 `DRIVER_SQLITE_PATH`、或文件不存在 → **启动即失败**，不静默建一个空库；
- **注册/注销不暴露 HTTP 接口**：那等于开放远程代码执行面，留给阶段 4 的权限与阶段 6 的远程插件；HTTP 只暴露只读的 `GET /v1/plugins`；
- SQLite 以 `file:...?mode=ro` 只读打开，BLOB 转 base64，未知表/未知列返回 422 并在 `details` 里给出可用表名/列名。

**验收证据**：`/v1/plugins` 返回 `sqlite 0.1.0` 及其工具清单；`/v1/tools` 含 `search_database`；一次带 `filters` 的查询返回结构化结果（含 BLOB → base64、`null` 保留）；未知表 → 422 `invalid_arguments`（`details[].type = unknown_table`）；pytest 56 passed；mypy 接入 CI。

**已知限制**：单连接串行；`total` 多一次 `COUNT(*)`。

## 阶段 3：协议适配层（`6ee7632`，2026-09-30）

**目标**：把同一份注册表同时以 MCP 与 OpenAI 两种协议对外呈现。

**交付物**：`protocols/{__init__,openai_schema,mcp,mcp_server,routes}.py`、`tools/executor.py`、`config.py` 增 `DRIVER_PROTOCOLS`、`docs/stages/stage-3.md`、AST 隔离测试。

**关键决策与理由**

- **用官方 `mcp` SDK，而不是手写 JSON-RPC**：MCP 是有版本的对外协议（`initialize` 协商、capabilities），手写只能对自家的测试全绿，对真实客户端半通；
- **`mcp` 作为可选依赖**（`driver-platform[mcp]`）：SDK 会拖进 cryptography / jsonschema / pydantic-settings / sse-starlette 一大套，只跑 HTTP 的部署不该背这个包袱，所以 `pip install driver-platform` 默认不带；`dev` extra 里带上，CI 才能跑一致性与端到端测试；
- **只做 stdio，不做 HTTP/SSE**：stdio 是本地 Agent 最普遍也最安全的接法（进程由 Agent spawn，无监听端口）；远程传输属于阶段 4/6，现在做半成品会被推倒；
- **抽 `tools/executor.py`**：HTTP 与 MCP 必须共用同一条执行管线，否则错误码迟早分叉；
- **错误映射分两套**：MCP 的工具级失败是 `CallToolResult(isError=True)`（模型能看见并自纠），协议层错误才用 `-32xxx`；HTTP 侧继续用阶段 1 的错误信封，两侧共用同一个 `to_payload()`；
- **协议默认关**：`DRIVER_PROTOCOLS` 留空则全关，写了不支持的协议名 → 启动即失败；
- **隔离用测试钉住**：AST 扫描全部源码，只有 `protocols/mcp.py` 与 `mcp_server.py` 允许 import `mcp`，OpenAI 适配器不许 import `mcp`/`openai`/`anthropic`/`langchain`，核心不许 import 任何具体适配器。

**验收证据**：真实 MCP 客户端（官方 SDK + stdio 子进程）完成 `tools/list` 与 `tools/call search_database`，返回结构化结果，中文错误信息在管道上原样往返；`GET /v1/protocols/openai/tools` 返回 `{"type":"function","function":{...}}` 且与 `/v1/tools` 的 `input_schema` 逐字一致；未开协议 → 404 `protocol_not_enabled`；缺可选依赖时进程退出并给出人话提示（不是 Traceback）；pytest 74 passed。

**已知限制**：只做 stdio 与 tools（无 HTTP/SSE、无 resources/prompts、一次一个客户端）；协议版本跟随 SDK。

## 阶段 4：安全权限（`e9750c1`，2026-09-30）

**目标**：无凭证拒绝、越权被拦，且 HTTP 与 MCP 共用同一套授权逻辑。

**交付物**：`security/{keys,policy,http}.py`、`errors.py` 增 `missing_credentials` / `invalid_credentials` / `permission_denied`、`config.py` 增 `DRIVER_AUTH_MODE` / `DRIVER_API_KEYS`、`api/tools.py` 与 `api/plugins.py` 按凭证过滤、`docs/stages/stage-4.md`。

**关键决策与理由**

- **凭证 = API Key + 工具级作用域**：`secret`（或 `secret:*`）= 全部工具，`secret:tool1|tool2` = 允许列表；钥匙内不得含 `:` `,` `|`，否则配置即失败 —— 用一个字符串把「谁的钥匙、能调什么」说清楚，不引入第二个配置文件；
- **`DRIVER_AUTH_MODE=off|required`，默认 `off`**：本地开发与 stdio（进程由 Agent 自己拉起、没有监听端口）不该被默认挡住；但**一旦带了凭证就必须有效**，绝不「凭证错就放行」；
- **`required` 却没有任何钥匙 → 启动即失败**：否则「开了鉴权」会变成人人都能调；
- **401 与 403 分语义**：认不出身份是 401（`missing_credentials` / `invalid_credentials`），认出来了但该工具不在作用域是 403（`permission_denied`）——不是 404，避免调用方把「越权」误当成「工具不存在」；
- **可见性随之收敛**：`GET /v1/tools` 与 `GET /v1/plugins` 只列该凭证作用域内的工具，Agent 没资格调的工具干脆看不见；
- **MCP 侧只放一把钥匙**：服务端清单是复数 `DRIVER_API_KEYS`，stdio 子进程从 `DRIVER_API_KEY`（单数）读自己那一把，走同一个 `authenticate` + 可见性策略；
- **`/health` 永远公开**：探活不该需要凭证。

**验收证据**：`DRIVER_AUTH_MODE=required DRIVER_API_KEYS="s3cret-full, s3cret-echo:echo"` 下 —— 无凭证 → 401 `missing_credentials`；坏凭证 → 401 `invalid_credentials`；`s3cret-echo` 访问 `search_database` → 403 `permission_denied`；`s3cret-echo` 的 `/v1/tools` 只列 `echo`，`s3cret-full` 则列全量；MCP 侧同钥匙同规则；pytest 98 passed；mypy 27 source files；ruff format 40 files。

**已知限制**：鉴权只到「工具名」这一层；密钥明文放在环境变量里；没有速率限制、配额、审计与吊销。

---

## 当前状态（截至 `e9750c1`）

- **已完成**：阶段 0 / 1 / 2 / 3 / 4，共 5 个提交，51 个受版本控制的文件；
- **门禁（最近一次全量验证）**：`ruff check .` → All checks passed；`ruff format --check src tests` → 40 files already formatted；`mypy src` → Success: no issues found in 27 source files；`pytest` → 98 passed；
- **包版本**：`pyproject.toml` 仍是 `0.0.0`，尚未随阶段递增（对外契约版本走各工具的 `contract_version`，与包版本解耦）；
- **下一步**：阶段 5「可观测性」—— 目标是 Grafana 看 QPS/延迟/错误率、日志按 `request_id` 追全链路。本机没有 Docker/Grafana，计划先做**进程内指标 + 结构化 JSON 日志 + `request_id` 贯穿 + `/metrics` 端点**，Grafana 面板留到阶段 7 的 Compose 一起验（该折衷待确认）。

## 已知限制（汇总）

1. CI 以 Python 3.14 为准，`requires-python` 放宽到 3.11+，但 3.11 / 3.12 未实测；
2. 工具执行没有超时与并发限制，详见阶段 1 说明；
3. SQLite 插件单连接串行、`total` 多一次 `COUNT(*)`，详见阶段 2 说明；
4. MCP 侧只做 stdio 与 tools，协议版本跟随 SDK，详见阶段 3 说明；
5. 鉴权默认关且只到工具名粒度，无速率限制/配额/审计/吊销，详见阶段 4 说明；
6. **本机开发环境的沙箱限制（不影响 CI）**：受限进程下运行带 `tmp_path` 的 pytest 需要提权 —— pytest 用 `Path.mkdir(mode=0o700)` 建临时目录，其 DACL 不含受限令牌的身份，导致 `PermissionError [WinError 5]`；同理，在本机验证 MCP stdio 端到端（匿名管道）也需要提权。CI（ubuntu-latest）不受影响。
