# 变更日志（CHANGELOG）

驱动平台自阶段 0 起的完整实现日志。每条对应一个提交，记录**目标 / 交付物 / 关键决策与理由 / 验收证据**；阶段 0–7 之外另有 1 条非阶段提交（工具链与环境约束，见文末）。

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
| 5 可观测性 | `80cd902` | 2026-10-01 | 24 files, +1925 / −48 | Grafana 看 QPS/延迟/错误率；日志按 `request_id` 追全链路 |
| 6 远程插件 | `a2a0abc` | 2026-10-01 | 17 files, +1824 / −17 | 远程注册即可调用；`DELETE` 下线后工具自动不可用 |
| 7 部署交付 | `803d3c6` | 2026-10-01 | 14 files, +1052 / −28 | `docker compose up` 起全平台；容器健康、指标被抓、面板有数 |
| 8 生产加固 | `153e209` | 2026-10-09 | 15 files, +1151 / −21 | 3 副本经网关滚动替换，8.7 万次请求 0 失败；Prometheus 三个副本 up=1 |
| 文档（任务书 + 变更日志） | `783b5c6` | 2026-10-01 | 4 files, +409 / −2 | `docs/task-brief.md` + `CHANGELOG.md` + README 入口 |
| — 工具链与环境约束 | 见文末 | 2026-10-01 | 6 files, +268 / −10 | 本地门禁全绿；沙箱三坑固化解法 |
| — PhoNote 插件 P0 | 见文末 | 2026-11-10 | 见文末 | 六个 `note_*` 工具；截图 → 缓冲区 → 组笔记 → 读回 |
| — PhoNote 插件 P1 | 见文末 | 2026-11-10 | 见文末 | 十七个工具 + `/notes/api/*` REST 层；树增删改移、批注、版本回滚、导出 |
| — PhoNote 插件 P2 | 见文末 | 2026-11-10 | 见文末 | AI 总结、讨论与增量补丁（`find` 唯一匹配才套用）；manual 模式不需要 key |
| — PhoNote 插件 P3 | 见文末 | 2026-11-13 | 见文末 | 备份 zip + WebDAV 同步：真实 HTTP 上 push → 换目录 pull，笔记与图片完整还原 |
| — 收口四项决策 | 见文末 | 2026-11-13 | 见文末 | CI 覆盖率硬门禁 + 安全扫描；笔记模板；Agent 只读窄钥匙 |
| — 收口八项：能力补齐 | 见文末 | 2026-11-13 | 见文末 | PostgreSQL 插件；包版本 0.1.0；远程插件重试与清单缓存；`/metrics` 可选鉴权；容器非 root + 只读根文件系统；截图默认压 WebP |
| — 收口五项：编排与分发 | 见文末 | 2026-11-13 | 见文末 | 3.11/3.12 CI 矩阵；K8s 清单；镜像 245→118 MB；GHCR 发布任务；W3C traceparent 跨进程；Electron 热键实测 |

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

## 阶段 5：可观测性（`80cd902`，2026-10-01）

**目标**：Grafana 上看得见 QPS / 延迟 / 错误率，日志能按 `request_id` 串起一次调用。

**交付物**：`observability/{context,metrics,logging,middleware,routes}.py`；`config.py` 增 `DRIVER_METRICS_ENABLED` / `DRIVER_LOG_LEVEL` / `DRIVER_LOG_FORMAT`；`main.py` 装配（日志配置 + 按开关挂中间件与 `/metrics`）；`errors.py` 信封增可选 `request_id`；`protocols/mcp.py` 每次 `tools/call` 发号；`tools/executor.py` 单点埋点；5 个新测试文件 + 3 处既有断言补齐；`docs/observability/{prometheus.yml,grafana-dashboard.json}`；`docs/stages/stage-5.md`。README 的路线表、目录结构、用法与已知限制同步更新。

**关键决策与理由**

- **指标默认关，日志与 `request_id` 常开**：`/metrics` 是一个无鉴权的新端点，多开一个端点就是多一块暴露面，所以要显式开；而结构化日志与请求号是零契约成本、又是排障刚需，没有理由默认关。
- **埋点只放一处（`tools/executor.py`）**：HTTP 与 MCP 共用同一条执行管线才能有同一个口径，以后接阶段 6 的远程插件也自动同源——不需要记得在第三个协议里补埋点。
- **`outcome` 复用对外错误码词汇**（`success` / `permission_denied` / `invalid_arguments` / `tool_not_found` / `tool_execution_failed` / `internal_error`）：能按 `code` 分支的地方就能按 `outcome` 建面板，不引入第二套词汇去维护映射表。
- **标签不许由调用方决定**：`route` 只取路由模板（用原始路径会让时间序列基数随调用方输入膨胀），未注册的工具名统一落 `unknown`（工具名来自请求体），原始名只进日志。
- **手写纯 ASGI 中间件，不用 `BaseHTTPMiddleware`**：后者把响应体包成流、contextvars 传播易踩坑，而且看不到 `http.response.start` 上的真实 status（`/metrics` 之外的错误状态码就记不准）。
- **只挂 `driver_platform` 一支 logger 且 `propagate` 保持真**：不去 root 上抢 handler，才不会和 uvicorn、pytest 的 `caplog` 打架；日志写 stderr——MCP 的 stdout 是协议通道，碰不得。
- **`/metrics` 与 `/health` 一样免凭证**：抓取方是 Prometheus 不是人；暴露面靠「默认关 + 阶段 7 的 compose 不把它映射到公网」来管，而不是靠再发一把钥匙。
- **信封里的 `request_id` 是可选扩展**：多一个字段不破坏既有调用方，MCP 的 stdio 没有响应头，错误信封是唯一能带号回去的地方。
- **`log_format` / `log_level` / `metrics_enabled` 的非法值一律启动即失败**：配置错误在启动期喊出来，比运行期发现日志格式没生效便宜得多。

**验收证据**：门禁全绿——`pytest` **167 passed**、覆盖率 **94%**（1021 语句 / 64 未覆盖），`ruff check .` All checks passed，`ruff format --check src tests` 51 files，`mypy src` 33 source files，`bandit -r src -q` exit 0；`/metrics` 开关开时返回四个指标的真实样本行、关时 404；`X-Request-ID` 原样透传或由服务端发 32 位 hex，工具 404 信封的 `request_id` 与响应头同值；Prometheus target `health=up`，取到 7 条 HTTP 序列与按 `tool,outcome` 的工具序列；Grafana 13.2.3 上数据源 `prom-main` + 面板 `driver-platform-overview`（4 个面板）导入成功，经其数据源代理执行四个表达式分别取到 QPS `0.2545`、P95 `0.00475`、错误率 `0`、`success/echo 0.00678`。逐条证据见[阶段 5 说明](docs/stages/stage-5.md)的「验收证据」。

**已知限制**：直方图用默认桶（5 ms–10 s）；不记请求/响应体；`request_id` 只在**本进程内**贯穿（没有 traceparent）；指标是进程内的，重启归零；框架级 500（由最外层 `ServerErrorMiddleware` 生成）没有 `X-Request-ID` 响应头——指标与日志仍带号。

---

## 阶段 6：远程插件（`a2a0abc`，2026-10-01）

**目标**：远程注册的插件可被调用，下线后工具自动不可用。

**交付物**：`plugins/remote.py`（远端契约 1.0 的客户端与工具适配）；`api/plugins.py` 增 `POST /v1/plugins/remote` 与 `DELETE /v1/plugins/remote/{plugin_id}`；`errors.py` 增 3 个错误码；`security/keys.py` 增 `plugin_admin` 作用域与 `can_manage_plugins`；`config.py` 增 `DRIVER_REMOTE_PLUGIN_ENABLED` / `DRIVER_REMOTE_PLUGINS` / `DRIVER_REMOTE_TIMEOUT`；`plugins/host.py` 增 `get()`；`bootstrap.py` 装载启动期远程插件；`httpx` 进核心依赖；`examples/remote_echo_service.py`（参考远端）；`tests/test_remote_plugin.py`（24 例）、`tests/test_remote_api.py`（20 例）、`tests/test_config.py` +7 组、`tests/test_protocol_isolation.py` 增 httpx 守卫；`docs/stages/stage-6.md`。README 的路线表、目录结构、用法与已知限制同步更新。

**关键决策与理由**

- **远端只要求 HTTP + JSON**：任何语言、任何框架只要实现 `GET /manifest` 与 `POST /execute` 就能接入，我们不必为每种语言发 SDK；代价是超时、错误映射要自己定，所以把这份契约钉成 1.0。
- **拉清单（平台拉）而不是推工具（远端上报）**：装载那一刻就知道工具身份，拿不到清单就不装载、发现冲突就整体回滚——失败发生在注册这一步，而不是第一次调用时。
- **不复刻 JSON Schema 校验器**：平台把远端的 `input_schema` 原样透出，参数由调用方按远端 schema 生成、远端自己校验，平台只把远端的错误信封原样透出。两份校验器必然漂移，而「谁是参数的真话」只能有一个答案：工具的提供方。
- **装载期同步、调用期异步**：`Plugin.setup()` / `teardown()` 是阶段 2 定下的同步接口（改异步是破坏性变更），所以拉清单用同步 `httpx.Client`；工具调用在事件循环里，用 `AsyncClient`，且**每次调用新建一个**——同步的 `teardown()` 关不掉它，代价是不复用连接池（记入已知限制）。
- **管理接口默认关，且与工具调用分权**：远程插件等于「运行时可注册的可执行入口」，默认关是「特性开关默认关」的落地；`plugin_admin` 不是工具名（带它的钥匙调不了任何工具），只有工具作用域的钥匙也管理不了插件。
- **管理端点始终注册，关着时 404 `remote_plugin_disabled`**：与 `/metrics` 的「关就不注册路由」刻意分歧——管理接口的调用方是程序，FastAPI 默认的 `{"detail":"Not Found"}` 会被读成 URL 写错，而 404 + 我们的信封能直接说清「要设哪个开关」。
- **卸载只认远程插件**：本地插件仍只由配置决定，避免「用远程接口管本地插件」——那样一份配置就有了两个真话。
- **远端错误码原样透出**：`code` 保留远端原文（`invalid_arguments` / `tool_not_found` / `permission_denied`），只做状态码映射（422 / 404 / 403，其余 502），所以调用方看到的词汇与平台自己抛的错误是同一套。
- **结果必须是 JSON 对象**：让「成功」与「失败」在类型上不会混；平台不二次校验、不改形状（`output_mode=None` 透传）。
- **启动期装载失败 = 进程起不来**：宁可起不来，也不要带着一半坏工具对外服务；代价是部署编排里必须安排启动顺序。

**验收证据**：门禁全绿——`pytest` **241 passed**（0 failed / 0 error）、覆盖率 **95%**（1248 语句 / 68 未覆盖，其中 `plugins/remote.py` 97%、`api/plugins.py` 98%），`ruff check .` All checks passed，`ruff format --check src tests examples` 55 files，`mypy src` 34 source files。真实 HTTP 全程（示例远端 9100 + 平台 8000）：无 `plugin_admin` 作用域的钥匙 **403**（details 写明需要的 scope）→ `admin-key` 注册 **201** → `GET /v1/tools` 里 `remote_echo` 的 schema 与远端清单逐字一致 → 调用 **200** `{"echo":"hi","times":3,"repeated":["hi","hi","hi"]}` → `times=99` 由远端拦下 **422** → `DELETE` **204** → 清单里消失、调用 **404 `tool_not_found`** → 开关关时 **404 `remote_plugin_disabled`**。与阶段 5 的接缝：远端工具调用照样产出 `event=tool_call` 日志（`request_id` 与响应头同值）与 `driver_tool_calls_total{tool="remote_echo"}`。逐条证据见[阶段 6 说明](docs/stages/stage-6.md)的「验收证据」。

**已知限制**：清单只在装载时拉一次（远端改 schema 需重新注册）；无重试、熔断、退避（远端抖动直接是 502）；远端是可执行信任边界，无签名与来源校验；平台照抄远端 `input_schema`，远端写错则调用方按错的生成；超时是单次请求超时；每次调用新建 HTTP 客户端（不复用连接池）；`base_url` 是单点；下线不通知远端；本地插件仍无 HTTP 管理接口。

---

## 阶段 7：部署交付（`803d3c6`，2026-10-01）

**目标**：`docker compose up` 一条命令起全平台（驱动平台 + SQLite 插件 + Prometheus + Grafana）；CI/CD 通过。

**交付物**：`docker-compose.yml`（三个服务 + 三个命名卷）；`Dockerfile`（分层构建 + `HEALTHCHECK` + 入口脚本）；`.dockerignore`；`deploy/docker/entrypoint.py`、`deploy/demo/seed_demo_db.py`、`deploy/prometheus/prometheus.yml`、`deploy/grafana/provisioning/{datasources,dashboards}/`；`.github/workflows/ci.yml` 新增 `deploy` 任务；`docs/observability/grafana-dashboard.json` 改为 provisioning-ready；`tests/test_deploy_config.py`（15 例）；`docs/stages/stage-7.md`。README 的路线表、目录结构、用法与已知限制同步更新；dev extras 增 `PyYAML`。

**关键决策与理由**

- **Compose 而不是 K8s / Helm**：本阶段要证明的是「一台机器、一条命令、可复现」，部署描述就是仓库里的一个文件；多副本、滚动更新、就绪探针是阶段 8 的事，到那时这份 compose 文件正好是对照物。
- **第三方镜像钉摘要，基础镜像用标签**：两者要的东西不同 —— `prom/prometheus`、`grafana/grafana` 要**行为可复现**（钉 `@sha256:…`，摘要记在环境手册 §2），`python:3.14-slim` 要**安全补丁自动进来**（用标签）。取舍记入已知限制。
- **本服务用 `build: .` 不推镜像仓库**：`docker compose build` 本身就是构建证明，CI 跑得通；推仓库要有 registry 凭据、tag 与版本号策略（包版本仍是 `0.0.0`），属于交付流水线的范围，本阶段不引入。
- **Grafana 用 provisioning 文件，面板只有一份真话**：数据源 `prom-main` 与四个面板都以仓库文件为准，`up` 之后打开就有；为此把阶段 5 那份面板 JSON 改成 provisioning-ready（去掉 `${DS_PROMETHEUS}` 占位符、固定 uid）——prov库isioning 不做占位符替换，留着占位符会静默变成「Datasource not found」。
- **容器入口脚本种一份演示库**：SQLite 插件是**只读**打开库的，没有种子文件就是「容器起来了、`search_database` 却查不了」。种子由 `DRIVER_SEED_DEMO_DB` 控制、文件存在则跳过（幂等），生产设 `0` 挂自己的库。
- **入口脚本用 Python 且 `os.execvp`**：Windows 上写 shell 脚本容易带 CRLF；`exec` 让 uvicorn 成为 PID 1，`docker stop` 的 SIGTERM 直达应用——阶段 8 的滚动更新要靠这条。
- **健康检查用 `python -c urllib`，容器里不装 curl**：镜像里本来就有 Python，为一个探针再拉一层不值得。`prometheus` 用 `depends_on: service_healthy` 等平台健康——否则启动初期看板上是一段 `up=0`。
- **部署契约用测试钉住**：「容器能起来」不能只在开发机验一次。15 个不起容器的用例守住三类静态错误：① compose 里每个 `DRIVER_*` 都必须是平台真会读的名字（打错一个字母就是启动失败或静默走默认值）；② 第三方镜像必须是摘要形态、`.dockerignore` 必须排掉 `.venv/`/`.git/`/`tests/`/`docs/`；③ Grafana 数据源 uid ↔ 面板 `datasource.uid` ↔ provisioning 挂载路径三处必须对上（对不上时 Grafana 不报错，只是面板没数据）。
- **CI 的 `deploy` 任务不起 Prometheus / Grafana**：拉两个第三方镜像对冒烟没有增益，纯粹花时间；冒烟用 `--no-deps` 只起平台，跑完 `down -v` 收尾。
- **Grafana 关闭自带插件的预装与自动更新**（`GF_PLUGINS_PREINSTALL_DISABLED` / `GF_PLUGINS_PREINSTALL_AUTO_UPDATE`）：Grafana 13 默认在启动时把自带插件升级到最新版，先停掉旧进程再从 `storage.googleapis.com` 下载；离线或受限网络下下载失败会**连带注销**自带的那份——包括 prometheus 数据源。这是本阶段真起全栈才暴露的缺陷，证据链与修复见阶段 7 说明的验收证据第 6 条。

**验收证据**：门禁全绿——`pytest` **256 passed**（0 failed / 0 error）、覆盖率 **95%**（1248 语句 / 68 未覆盖），`ruff check .` All checks passed，`ruff format --check src tests deploy examples` 58 files，`mypy` 34 source files。本机 compose 全栈（Docker Desktop，engine 29.8.1 / compose v5.5.1）：`config --quiet` / `build` / `up -d` 全部 exit 0，`docker compose ps` 三个服务 `Up`（平台 `healthy`），命名卷里 `tables=['customers','orders']`、`customers rows=4`；`GET /health` → 200；带 `read-key` 调 `search_database` 查上海 → 200 `row_count=2` 的两行真数据，去掉钥匙 → 401；Prometheus target `http://driver-platform:8000/metrics` `health=up`，查询 `driver_tool_calls_total` → `{instance="compose",job="driver-platform",outcome="success",tool="search_database"} 1`；Grafana `/api/health` ok、数据源 `prom-main`（`readOnly=true`，来自 provisioning）、面板 `driver-platform-overview` 四个面板全指向 `prom-main`，经 `/api/ds/query` 与数据源代理都能取到数；容器日志是阶段 5 的 JSON 契约，`event=tool_call` 带 `request_id` 与 `duration_ms`，抓 `/metrics` 的来源是 `172.18.0.3`（Prometheus 容器）；CI 上 `test` 与 `deploy` 两个任务均 **success**（提交 `a97e3ab`，[run 36832468903](https://github.com/qw2007/DRiver-api/actions/runs/36832468903)）。逐条证据见[阶段 7 说明](docs/stages/stage-7.md)的「验收证据」。

**已知限制**：单副本（重启期间不可用）；容器以 root 跑、无资源限额、无只读根文件系统；第三方镜像钉的是 amd64 平台摘要；基础镜像用标签；compose 里的演示钥匙是明文；`/metrics` 无鉴权且暴露在宿主端口；Prometheus 只在平台健康后才开始抓取；Grafana 首次启动在 WSL 卷上迁移极慢（本机约 6.5 分钟，卷建好后约 20 秒）；`docker compose down -v` 会删三个数据卷；演示库种子只认 SQLite；provisioned 面板不允许 UI 修改；Grafana 自带插件不能运行时自动更新（只能改镜像摘要）。

---

## 阶段 8：生产加固（`153e209`，2026-10-09）

**目标**：多副本；滚动更新不中断；旧版 Agent 兼容。

**交付物**：`docker-compose.scale.yml`（overlay：摘端口 + nginx 网关）；`deploy/nginx/conf.d/default.conf`；`deploy/rollout.py`、`deploy/traffic_probe.py`；`deploy/prometheus/prometheus.yml` 改 `dns_sd_configs`；`Dockerfile` 加 `--timeout-graceful-shutdown` 与构建戳 `ARG`/`LABEL`；`src/driver_platform/main.py` 加 lifespan；`deploy/demo/seed_demo_db.py` 改原子安装；`tests/test_compat.py`、`tests/test_lifecycle.py`、`tests/test_scale_config.py`（新增）与 `tests/test_deploy_config.py`、`tests/conftest.py`（改）；`.github/workflows/ci.yml` 的 `deploy` 任务加静态校验；`docs/stages/stage-8.md`。README 的路线表、用法、目录结构与已知限制同步更新。

**关键决策与理由**

- **多副本用 Compose `--scale` + nginx，不是 K8s**：compose 已经有服务名 DNS、健康门与 `--scale` 三个原语，而阶段 7 那份单副本 compose 正好是它的对照物；换 K8s 会把「阶段 7 的部署描述」变成一次性产物。overlay 文件而不是改基础文件：单副本用法一字不动。
- **网关只做解析，不做服务发现**：`resolver 127.0.0.11` + 变量式 `proxy_pass http://$backend:8000` 让 nginx 每次请求问 Compose 内置 DNS，副本 IP 变了不用 reload，也不用维护 `upstream` 块。
- **优雅停机靠 uvicorn + lifespan，不自己写信号处理**：入口脚本 `os.execvp` 已让 uvicorn 是 PID 1，再注册 SIGTERM handler 就是和它抢活。两级窗口各司其职——`--timeout-graceful-shutdown 20` 管「最多等多久」，lifespan 的退出阶段管「等的时候做什么」（`PluginHost.close()` 逆序卸载插件、逐个尽力而为）。
- **滚动必须能被测量，所以写成脚本**：Compose 非 swarm 模式没有原生 rolling（`deploy.replicas` 被忽略、`up -d` 是先停后起），「不中断」就成了一句无法反驳的话。`rollout.py` 把「停一个 → 补一个 → 等健康」做成可重复序列，`traffic_probe.py`（纯标准库，本机没有 ab/hey/k6）在窗口内持续打流量并报成功/失败与状态码分布——**0 失败是可以被复现的断言，不是感觉**。
- **探针走网关而不是某个副本**：它衡量的是「客户端视角有没有断」；直接打副本只能证明「那个副本当时活着」。
- **兼容靠「把隐式现状钉成测试 + 写下策略」，不加版本协商中间件**：真正脆弱的地方是没人知道 pydantic 默认 `extra='ignore'`（顶层与工具入参的未知字段都被忽略）算不算承诺——于是把它写成 `tests/test_compat.py` 的断言，并在阶段说明里给出 1.x 能加什么、什么必须升主版本的判据。中间件只能拦住它自己知道的字段。
- **抓全副本改 `dns_sd_configs`**：副本 IP 是动态的，`static_configs` 写死一个就只抓一个；`dns_sd` 一份配置同时服务单副本与多副本，`instance` 默认是「目标地址」，正好用来区分副本。
- **演示库种子改原子安装**：多副本共挂同一数据卷，N 个容器同时启动都会看到「库不存在」；直接写同一路径会 `database is locked` 或留下半成品库，而平台是**只读**打开的——之后谁都查不了。改成「同目录临时文件 + `os.replace`」，要么完整可见、要么不存在。
- **镜像加构建戳**：同源重建命中构建缓存时镜像 ID 不变，光看构建输出分不清新旧，「替换真的换了镜像」就无从核对；`ARG DRIVER_BUILD_STAMP` + `LABEL` 让这件事在镜像层可查。
- **CI 只做静态校验（overlay `config --quiet`、`nginx -t`、脚本 `py_compile`）**：真滚动要拉镜像、起网关、跑分钟级探针，对冒烟没有增益；能进 CI 的是「配置能合并、nginx 配置合法、脚本没写坏」。

**验收证据**：门禁全绿——`pytest` **276 passed**（0 failed / 0 error）、覆盖率 **95%**（1257 语句 / 65 未覆盖），`ruff check .` All checks passed，`ruff format --check src tests deploy examples` **63 files**，`mypy` **34 source files**。本机 3 副本 + gateway 滚动验收（脚本自身 exit 0）：构建带戳新镜像（`fcb83fa59641` → `fad8a0d26702`，`image-changed: True`），三个副本逐个 `stop --time 25` → `rm` → `up -d --no-deps --no-recreate --scale driver-platform=3`，**每一步替换后都是三个 healthy 且新旧镜像并存**；经网关的探针 `{"total": 86875, "ok": 86875, "failed": 0, "statuses": {"200": 86875}, "duration_ms": {"min": 1.863, "median": 3.090, "max": 2014.668}}`（上一轮同序列是 91671 / 0 失败）；网关 `GET /health` → `{"status":"ok"}`、`POST /v1/tools/execute`（`read-key`）→ `{"tool":"echo","contract_version":"1.0","result":{"echo":"rollout","times":1}}`。Prometheus 重载 `dns_sd_configs` 后 `targets-count=3`、三个副本 `health=up` 且 `instance` 各不相同，`up{job="driver-platform"}` 三条序列值全为 1。优雅停机证据：`docker stop` 后容器日志依次出现 `INFO: Shutting down` → `INFO: Waiting for application shutdown.` → `{"logger": "driver_platform.main", "msg": "进程收尾：卸载插件（逆序，逐个尽力而为）"}` → `INFO: Application shutdown complete.`，且 `docker stop` 未等到 25 秒上限就返回。逐条证据与「本阶段自己踩出并修掉的 9 个真实缺陷」见[阶段 8 说明](docs/stages/stage-8.md)的「验收证据」。CI（提交 `894ff44`，[run 37953653712](https://github.com/qw2007/DRiver-api/actions/runs/37953653712)）：`test` 与 `deploy` 两个任务均 success，其中 `deploy` 新增的「多副本 overlay / `nginx -t` / 验收脚本 `py_compile`」校验步骤也通过。

**已知限制**：`--scale` 不是编排（不自动补副本、不保证分散到不同宿主、无滚动策略）；nginx 解析有 5 秒窗口（靠 `proxy_next_upstream` 重试兜底，代价是多一跳）；重试对非幂等请求也开着（当前工具全是只读查询才安全）；单机多副本共享同一份只读数据卷（写型插件不安全）；探针只衡量成功率不衡量延迟分布；优雅停机上限 20 秒，超时会被强杀；镜像仍以 root 运行、仍绑基础镜像标签、只验证过 amd64（阶段 7 的限制延续）；`/metrics` 经网关暴露且仍无鉴权；滚动脚本不做版本并存的双写/流量着色；远程插件的 HTTP 客户端会继承宿主机代理设置（私网部署应显式配 `NO_PROXY`）。

---

## 工具链与环境约束（非阶段交付，2026-10-01）

**目标**：为阶段 5 备齐工具，并把本机开发环境的坑一次性固化下来——**不改变任何对外行为**。

**交付物**：`pyproject.toml`（新增依赖）、`docs/environment.md`（新增：环境与工具手册）、`README.md`（项目级文档入口 + 检查命令）、`CHANGELOG.md`（本节）、`src/driver_platform/plugins/sqlite.py`（安全扫描误报就地标注）、`.gitignore`（覆盖率 / pip-audit 缓存 / pytest 残留）。

**关键决策与理由**

- **只补真正要用的**：运行时加 `prometheus-client`（阶段 5 的 `/metrics`）；dev 加 `pytest-cov`、`bandit`、`pip-audit`（覆盖率 + 两项安全门禁）。`uv` / `poetry` / `pre-commit` / `kind` / `helm` / 各类数据库 CLI **一律不装** —— 没有对应阶段需求，装了只是维护面。
- **安全检查进常规门禁**：`bandit -r src` 与 `pip-audit --cache-dir .pip-audit-cache` 写进 README 检查命令。`sqlite.py` 初报 2 条 `B608`（SQL 注入）**确认为误报**：表名与列名都先过白名单校验（`self._tables()` / `available`），拼进 SQL 的标识符经 `_quote_identifier()` 双引号包裹并转义 `"`，所有**值**一律 `?` 绑定，库以 `mode=ro` 只读打开 → 就地 `# nosec B608` 并写明理由，比全局跳过 B608 更窄、也不掩盖将来的真问题。
- **GitHub 直连而不是走镜像**：`~/.gitconfig` 原有 `url."https://ghproxy.net/https://github.com/".insteadOf`，读能用但 push 必被 GitHub 拒（`Invalid username or token`）。本机 TLS 卡在**吊销检查**而非证书链，故改为 `http.sslBackend=schannel` + `http.schannelCheckRevoke=false` 并移除镜像改写；备份留在 `~/.gitconfig.bak-20261001-120108`。
- **gh / jq 不走 Chocolatey**：非提权进程写不了 `C:\ProgramData\chocolatey\lib-bad`（UAC 过滤令牌），改为从 GitHub Releases 解压到 `D:\DSh\tools\bin` 并追加**用户** PATH（不需要管理员）。
- **Docker 镜像走国内加速**：`~/.docker/daemon.json` 增 `registry-mirrors` 后拉取正常（直连 `registry-1.docker.io` 被 reset）。
- **两个镜像要钉摘要**：`prom/prometheus` 与 `grafana/grafana` 目前是 `latest`，阶段 5 组 Compose 时必须换成手册 §2 记录的 digest，否则「可复现部署」不成立。

**验收证据**：`ruff check .` → All checks passed；`ruff format --check src tests` → 40 files already formatted；`mypy src` → Success: no issues found in 27 source files；`bandit -r src -q` → exit 0；`pip_audit --cache-dir .pip-audit-cache --progress-spinner off` → No known vulnerabilities found；提权全量 `pytest -p no:cacheprovider` → 98 passed（覆盖率 **92%**：773 语句 / 63 未覆盖）；`gh --version` → 2.102.0、`jq --version` → 1.8.2（`echo '{"a":{"b":42}}' | jq .a.b` → `42`）；`docker images` → prom/prometheus 381 MB + grafana/grafana 1.94 GB；`git ls-remote origin` → `783b5c6 refs/heads/master`（exit 0，未再传任何 `-c` 参数）。

**已知限制**：以上修正都只作用于**本机**；CI 不需要提权、不需要 schannel、不需要镜像加速。受限模式下带 `tmp_path` 的 pytest 仍有 30 个用例失败，根因（`0o700` 目录的 DACL 对受限令牌 deny-only）与提权清单一并写在[环境与工具手册](docs/environment.md)。

---

## 非阶段交付：PhoNote 插件 P0（2026-11-10）

**目标**：把「网课截图 → 图片缓冲区 → 任意多图组成笔记 → 读回」做成平台插件，验证插件契约能承载一个真正的业务领域。设计见 [PhoNote 仓库的设计文档](https://github.com/qw2007/PhoNote/blob/master/docs/design/PhoNote.md)。

**交付物**：`src/driver_platform/plugins/pho_note/{__init__,plugin,store,tools}.py`、`tests/test_pho_note_store.py`（19 例）、`tests/test_pho_note_tools.py`（5 例）；`config.py` 新增 `DRIVER_NOTES_DIR`；`catalog.py` 注册 `pho_note` 工厂；`errors.py` 新增 `note_not_found`(404)。

**关键决策与理由**：
- **单表自引用树**（`nodes.parent_id` + `kind ∈ {folder,note}` + `position` 整数）：层级任意深、改名/移动不改任何路径，面包屑与导出用递归 CTE 现算——不存在「路径字段与真实位置不一致」这种 bug；
- **同层唯一名**（`UNIQUE(IFNULL(parent_id,''), name)`）由 SQL 兜底，重名自动加 ` (2)`：约束放数据库而不是校验代码，因为绕过 API 的写入路径以后一定会有；
- **图片内容寻址**（`media/ab/cd/<sha256>.<ext>`）：同一张图截两次只落一份盘，去重不需要额外逻辑；写入用同目录临时文件 + `os.replace`，崩溃不会留下半截文件；
- **六个工具而非设计里的二十六个**：P0 只挂当前真能兑现的能力——工具清单里出现调不通的工具，比少一个更糟；批注/版本/AI/同步随 P1–P3 加表加工具；
- **只接受 PNG/JPEG/WebP 魔数**，不猜扩展名：认不出就 422，而不是让浏览器渲染成一片空白；
- **`note_not_found` 与 `tool_not_found` 分开**：一个是「数据里没这条」，一个是「工具没注册」，调用方要做的补救完全不同；
- **不预埋空表**（无 `annotations` / `revisions` / `messages`）：`note_get` 因此不返回两个永远是 `[]` 的字段。

**验收证据**：`ruff check .` → All checks passed；`ruff format --check src tests deploy examples` → **69 files**；`mypy` → Success: no issues found in **38 source files**；`pytest` → exit 0，**300 passed**，覆盖率 **95%**（1561 语句 / 71 未覆盖，`pho_note/store.py` 98%、`tools.py` 99%）；真实 uvicorn 冒烟：`/v1/tools` 列出 `echo,note_add_capture,note_compose,note_get,note_list_captures,note_node_create,note_tree`，`note_add_capture` → `cap_36775eef78dd4ea8 image/png bytes=70`，`note_compose` → `nod_814c6c7173cd4da4 images=1`，`DRIVER_NOTES_DIR` 下生成 `phonote.db`。

**已知限制**：P0 无 REST 层（壳走 `/v1/tools/execute` 带 base64，单图上限 8 MB）；无改名/移动/删除；无批注与版本；AI 与同步未接；图片只能按 sha256 从磁盘读（`/captures/{id}/image` 属 P1）；多屏只截第一屏。

---

## 非阶段交付：PhoNote 插件 P1（2026-11-10）

**目标**：把 P0 的「六个工具」补齐到可以日常用——树的增删改移、批注、版本回滚、导出，以及给壳用的 REST 层（P0 的壳靠 `/v1/tools/execute` 传 base64，图片一多就慢）。

**交付物**：`store.py` 追加 `annotations` / `revisions` 两表与十二个方法（`SCHEMA_VERSION=2`）；`tools.py` 从十七个类压成「一张表 + 一个通用工具类」（十七个工具）；新建 `plugins/pho_note/api.py`（`/notes/api/*` 十六条端点，`DRIVER_NOTES_UI=1` 才挂）；`errors.py` 新增 `node_name_conflict`(409)、`node_move_invalid`(422)；`tests/test_pho_note_p1.py`（11 例）。

**关键决策与理由**：
- **加表不加 ALTER**：两张新表都用 `CREATE TABLE IF NOT EXISTS` 写进同一个 schema 脚本，`PRAGMA user_version` 只当标记——已经存在的 v1 库打开后自动多出两表，省掉迁移框架（真需要改列时再上）；
- **`note_set_images` 取代设计里的 attach/detach**：增、删、重排都是「整体重写」，一个工具做三件事，少一个「先删后加顺序乱了」的状态；
- **改正文先存旧版**：`note_update` 在覆盖前把旧正文写进 `revisions`，回滚本身也留一个版本——「撤销撤销」是真实需求；
- **改名撞车用 409 而不是 422**：请求本身合法，只是与现有数据冲突，两个错误码让调用方能区分「参数写错」和「名字被占」；
- **REST 与工具各管各的授权**：工具按工具名作用域，REST 按 `notes_read`/`notes_write`——只读钥匙因此可以给第二台设备/投屏，而它调不动任何写工具；
- **上传仍是 JSON base64**：P1 不引 `python-multipart`，单图 8 MB 上限对网课截图足够，少一个依赖与一条 multipart 解析路径；
- **通用工具类**：`_FnTool` 用实例方法覆盖 `manifest()`（与远程插件同一套做法），十七个工具从十七个类变成一张表，加工具是加一行。

**验收证据**：`ruff check .` → All checks passed；`ruff format --check src tests deploy examples` → **71 files**；`mypy` → Success: no issues found in **39 source files**；`pytest` → exit 0，**311 passed**，覆盖率 **95%**（1856 语句 / 98 未覆盖，`pho_note/api.py` 91%、`store.py` 94%、`tools.py` 99%）；真实 uvicorn（8012，`DRIVER_NOTES_UI=1`）跑通：建目录 `nod_185ebec0a5e445e5` → 上传 `cap_96378584d17f406e image/png` → 组笔记 `nod_f6b939947cc24cf8 images=1` → `PATCH` 后 `revisions` 有一条旧正文 → 批注 `ann_cae5ac922db54d60` → 导出响应头 `filename*=UTF-8''%E7%AC%AC%201...` 且 Markdown 里是绝对图片路径 → `/captures/{id}/image` 返回原字节。

**已知限制**：批注锚点里的「选中文字/图片区域」坐标由 P2 的 UI 补（REST 只存 JSON）；图片仍只能从磁盘按 sha256 读，无缩略图；导出是「复制 Markdown 到剪贴板 + 绝对路径」而不是打包下载；删除不级联删图（图留在缓冲区）。

---

## 非阶段交付：PhoNote 插件 P2（2026-11-10）

**目标**：把「看网课」这条链路补到能自己写笔记——让模型读图读文给出总结与增量补丁，人在讨论区继续追问，补丁逐条确认后落盘。对应设计文档 §8。

**交付物**：新建 `plugins/pho_note/ai.py`（`AIConfig`、`LLMProvider` 协议、OpenAI 兼容 / Anthropic / manual 三实现、`parse_reply`、`apply_edits`、`NoteAI` 门面）；`store.py` 加 `messages` 表（`SCHEMA_VERSION=3`）与 `add_message`/`list_messages`/`media_bytes`；`tools.py` 加五个 AI 工具（总数 22）；`api.py` 加四个 AI 端点；`config.py` 加五个 `DRIVER_NOTES_LLM*` 开关与校验；`errors.py` 加 `ai_not_configured`(400)/`ai_unavailable`(502)/`ai_reply_unparsable`(422)；`tests/test_pho_note_p2.py`（24 例）；壳侧讨论区接上总结/追问/补丁预览。

**关键决策与理由**：
- **模型只回补丁、不回全文**：`{"summary", "edits":[{"find","replace"}]}` 让「模型改了什么」可以逐条展示与拒绝；回整个正文会让用户在长笔记里找不到改动点；
- **`find` 必须恰好出现一次**：出现 0 次说明模型记错了原文，出现 2 次以上不知道该改哪处——两种都整条跳过并进 `skipped`，宁可不改也不改错；
- **`find == ""` 约定为追加**：这是唯一不依赖「原文里有锚点」的改法，总结类补丁常用；
- **manual 模式是一等公民**：不联网、不花额度，把正文 + 批注 + 图片路径打成上下文包，人粘给网页版模型再粘回来（`note_ingest_ai_reply`）——没有 API key 也能用完整条链路；
- **三模式的 HTTP 出口集中到 `_post`**：超时、连接失败、非 2xx、非 JSON 都映成 `ai_unavailable`，且**不回显响应体**（可能含原文与 key 指纹）；测试用 `httpx.MockTransport` 注入，零网络；
- **出网白名单从一处变两处**：`tests/test_protocol_isolation.py` 现在允许 `plugins/remote.py` 与 `plugins/pho_note/ai.py` 两处 import httpx，加第三处必须改这张表；
- **讨论历史落库而不是只放内存**：`messages` 表按 `created_at` 升序读回，壳关掉再开还能看到上下文；
- **REST 与工具共用处理器**：`/summarize`、`/chat` 直接调 `run_summarize`/`run_chat`，避免「HTTP 一套逻辑、工具另一套」慢慢分叉。

**验收证据**：`ruff check .` → All checks passed；`ruff format --check src tests deploy examples` → **73 files**；`mypy` → Success: no issues found in **40 source files**；`pytest` → exit 0，**335 passed**，覆盖率 **95%**（2221 语句 / 120 未覆盖，`ai.py` 92%、`api.py` 92%、`tools.py` 98%、`store.py` 94%）；真实 uvicorn（8011，`DRIVER_NOTES_LLM=manual`）：`/v1/tools` 23 个工具 → `POST /notes/api/nodes` 建 `nod_dd35be07a33b40c8` → `/summarize` 返回 `mode=manual copy_len=281` → `note_ingest_ai_reply` 套用补丁后 `body=第二行：补丁` 且 `revisions` 多一条 `ai-reply` → `/messages` 有一条 assistant。

**已知限制**：`ai.py` 只实现两家协议，没有重试、限流与流式；随请求最多带 6 张图（`DRIVER_NOTES_LLM_MAX_IMAGES`），长笔记可能超出模型上下文；批注仍只以 JSON 存锚点；壳的 Electron 仍未在本机启动过、`npm install` 未验证。

---
## 非阶段交付：PhoNote 插件 P3（2026-11-13）

**目标**：让笔记能在设备间搬——先把「一份自包含的备份」做出来（`note_backup`），再把备份推到 WebDAV（`note_sync_push`）与从远端还原（`note_sync_pull`）。对应设计文档 §7 与 §9。

**交付物**：新建 `plugins/pho_note/sync.py`（`SyncConfig`、`WebDAVClient`（`MKCOL`/`PUT`/`GET`/`DELETE` 四个动词够用）、`push`/`pull`/`status`、远端 `manifest.json`、本地 `sync.json` 状态）；`store.py` 加 `reload`/`snapshot_bytes`（`VACUUM INTO`）/`media_files`/`fingerprint`/`backup_db`/`backup_zip`；`errors.py` 加 `sync_not_configured`(400)/`sync_failed`(502)；`config.py` 加四个 `DRIVER_NOTES_WEBDAV_*` 开关与校验；`tools.py` 加四个工具（总数 26）；`api.py` 加 `POST /backup`、`GET /sync/status`、`POST /sync/push`、`POST /sync/pull`；`tests/test_pho_note_p3.py`（12 例）+ `tests/test_config.py` 补 5 例 WebDAV 配置；出网白名单加第三处 `plugins/pho_note/sync.py`。

**关键决策与理由**：
- **manifest 最后写**：远端 `phonote/manifest.json` 是提交点，先传媒体与快照、最后改 manifest；中途断网只会留下没人引用的孤儿文件，不会让某台设备 pull 到半套数据；
- **只传远端缺的媒体**：按 sha256 逐个 `GET` 判定，图片是内容寻址的，重复推送不会重复上传（第二台设备只拉自己缺的那几张）；
- **快照用 `VACUUM INTO`、按 `KEEP` 轮换**：直接拷 `phonote.db` 会漏掉 WAL 里的已提交页；`VACUUM INTO` 出的是一致副本，名字带 UTC 时间戳与 sha8 前缀，超过 `KEEP` 的旧快照在 manifest 落定后删；
- **脏检查比内容指纹、不比文件 hash**：`VACUUM INTO` 的快照与活动库**字节不同**（页布局会重排），拿文件 sha256 做判据会导致「刚 push 完就 pull 也被拒」；改成 `COUNT(*)` 与 `MAX(updated_at)` 组成的内容指纹，VACUUM/重开/WAL checkpoint 都不影响它，任何一次写都会变；
- **pull 先备份再原子替换**：先把当前库落到 `backups/pre-pull-<时间戳>.db`，关连接、删 `-wal`/`-shm`、`os.replace` 覆盖、再重开——中途失败仍能人工捡回旧库；
- **单主设备、不做双向合并**：本地有未推送改动时 pull 直接拒绝（`sync_failed`），而不是尝试字段级合并；冲突合并的成本远超「一个设备主导写、其他设备只读」这个真实用法；
- **同步状态放本地 `sync.json`**：远端只存数据、不存「谁推到哪」；每台设备自己记 `pushed_fingerprint`/`pushed_at`，避免多个设备争写同一份状态。

**验收证据**：`ruff check .` → All checks passed；`ruff format --check src tests deploy examples` → **75 files**；`mypy` → Success: no issues found in **41 source files**；`pytest` → exit 0，**354 passed**，覆盖率 **95%**（2495 语句 / 137 未覆盖；`pho_note/sync.py` 90%、`store.py` 95%、`tools.py` 98%、`api.py` 93%）。真实 HTTP（本机起 stdlib WebDAV 假服务，`NO_PROXY=127.0.0.1`，内存字典存对象）：

```
push: {"pushed": true, "snapshot": "db/20261113T113755Z-de19b506.db", "db_sha256": "de19b506...4f1e49bd", "uploaded_media": 1, "media_total": 1, "pruned": []}
pull: {"pulled": true, "snapshot": "db/20261113T113755Z-de19b506.db", "db_sha256": "de19b506...4f1e49bd", "backup": ".tmp\sync-smoke\two\backups\pre-pull-20261113T113756Z.db", "fetched_media": 1, "media_total": 1}
restored: {"name": "第一讲", "body_md": "触发器与视图", "images": 1, "image_on_disk": true, "tree": ["课程"], "dirty_before": false, "dirty_after": false}
```

即：目录 `one` 里建目录 + 上传一张图 + 组一篇笔记 → push 传 1 张媒体与 1 份快照 → 全新的目录 `two` 里先备份再 pull → 笔记正文、标题、图片文件与树结构全部还原，pull 前后 `dirty` 都是 `false`。

**已知限制**：只实现 `MKCOL`/`PUT`/`GET`/`DELETE` 四个动词（够用，但没有 `PROPFIND` 列目录、不支持 `MOVE`）；没有断点续传与分块上传，快照大了就是一次性 PUT；远端只增不删媒体（本地删掉的图仍留在远端，靠 `KEEP` 轮换快照间接收敛）；不做双向合并、单主设备；`sync.json` 是本地状态，换设备后第一次 `status` 会显示「未推送」但不影响 pull；**真实的坚果云/Nextcloud 都没连过**，只用本机写的假 WebDAV 服务（真 HTTP）验过。

---

## 非阶段交付：收口四项决策（2026-11-13）

**目标**：把当年挂在 `docs/handoff.md` 第 3.3 节的四个待拍板项一次性拍死，并落地其中需要写代码的两项。

**四条结论**：① CI 加覆盖率硬门禁（`--cov-fail-under=90`）；② `bandit` + `pip-audit` 进 CI；③ 不做 K8s 编排层（保留阶段 8 的单机 compose `--scale`）；④ 不换技术栈（继续 Python + FastAPI）。

**交付物**：
- `.github/workflows/ci.yml` 的 `test` job：`pytest` 改成 `pytest --cov=driver_platform --cov-report=term-missing:skip-covered --cov-fail-under=90`，并新增 `bandit -r src -q` 与 `python -m pip_audit --progress-spinner off` 两步；
- `plugins/pho_note/store.py` 新增 `TEMPLATES`（内置 `lecture` 骨架：定义 / 定理 · 结论 / 易错点 / 例题，共 4 个二级标题）与 `create_node(..., template=...)`（正文非空时以正文为准；目录套模板或未知模板 → `invalid_arguments`）；
- `note_node_create` 与 `POST /notes/api/nodes` 都加 `template` 字段；壳的设置里加「新建笔记套用骨架」勾选（默认开）；
- 文档：`docs/handoff.md` 3.2 的前两条改成「已补」、3.3 改写成结论表；PhoNote 仓库 `docs/design/PhoNote.md` 第 14 节从「未决」改成结论表。

**关键决策与理由**：
- **阈值卡 90% 而不是 95%**：本地长期 94–95%，卡 95% 会让每个小改动都要补测试才能合入；90% 是「别一路掉下去」的底线，真正的质量靠 review；
- **安全扫描进 CI 而不是只在本机跑**：本机跑过一次没有任何回归价值，只有每次提交都跑，依赖漏洞与 `# nosec` 误用才会被拦住；
- **模板只做一份且按需套用**：网课笔记的共性就是「抄定义、记定理、标易错点」，给十份花模板不如给一份能删的骨架；显式传 `body_md` 时模板不覆盖用户内容；
- **Agent 钥匙不新增机制**：作用域本来就支持任意工具名白名单，只需要把推荐写法（`pho-agent:note_tree|note_get|note_list_captures`）钉进测试与文档，避免有人给它 `notes_write`。

**验收证据**：`ruff check .` → All checks passed；`ruff format --check src tests deploy examples` → **75 files**；`mypy` → Success: no issues found in **41 source files**；`pytest --cov --cov-fail-under=90` → exit 0，**357 passed / 0 failed**，覆盖率 **94%**（2506 语句 / 138 未覆盖），`Required test coverage of 90% reached. Total coverage: 94.49%`。新增用例 3 个：工具层模板套用/正文优先/目录拒绝、REST 模板与未知模板 422、Agent 窄钥匙可读不可写。

**已知限制**：模板只有一份内置的（没有用户自定义模板的存储与界面）；`keep_original`（按次保留原图）在这次只写了结论、仍未实现；CI 的 `pip-audit` 依赖 PyPI 可达，离线环境会红。

---
## 非阶段交付：收口八项——能力补齐（2026-11-13）

**目标**：把 [`docs/handoff.md`](docs/handoff.md) 3.2 的九条留白按「哪条真会挡住下一步」排序后一次做掉，同时把「已定未做」的原图保留开关补上。

**交付物**：
- **A2 PostgreSQL 插件**（`src/driver_platform/plugins/postgres.py`）：`search_postgres`，入参输出复用 `SearchDatabaseInput`/`SearchDatabaseOutput`——Agent 看到的形状与 `search_database` 完全一致，只是后端换成 PostgreSQL 只读会话（`options=-c default_transaction_read_only=on`、`connect_timeout=5`）；表与列的元数据查 `information_schema`，动态 SQL 走 `%s` 占位符 + 双引号标识符；`psycopg` 是可选依赖（`pip install 'driver-platform[postgres]'`），没装时插件装载直接失败且给出安装提示；新增 `DRIVER_POSTGRES_DSN`。
- **A4 版本与依赖**：包版本 `0.0.0` → **`0.1.0`**（`pyproject.toml` 与 `src/driver_platform/__init__.py` 同一处真话，compose 镜像标签跟着走，测试守三点一致）；新增可选依赖组 `postgres` 与 `images`（Pillow）。
- **A5 远程插件健壮性**：清单缓存 60 秒（`MANIFEST_CACHE_SECONDS`）、连接失败重试一次（`RETRY_ATTEMPTS=2`/`RETRY_DELAY=0.2`），但**只对「请求根本没到远端」的 `ConnectError`/`ConnectTimeout`/`PoolTimeout` 重试**——读超时与 5xx 不重试，否则写操作会执行两遍。
- **A6 `/metrics` 可选鉴权**：`DRIVER_METRICS_PROTECTED=1` 时 `/metrics` 走与 `/v1/tools` 同一套凭证（无钥匙 401、有效钥匙 200）；默认仍公开（抓它的是集群内 Prometheus）。
- **A7 部署加固**：镜像里新建 `uid=10001(driver)` 并 `USER driver`、`chmod -R a-w /app`、`VOLUME ["/data"]`（只读根文件系统下 `/data` 必须是卷，否则种子脚本报 `Read-only file system`）；compose 加 `read_only: true`、`tmpfs: /tmp`、`no-new-privileges:true`、`mem_limit: 512m`、`cpus: "1.0"`；新增 `.env.example`，`.env` 进 `.gitignore`。
- **A3 类型检查扩围**：`mypy` 的 `files` 从 `["src"]` 扩到 `["src", "deploy", "examples"]`（不扩 `tests/`：mock 噪音大于收益），修掉 6 个真实类型问题（含 `traffic_probe.py` 用 `TypedDict` 描述汇总、`entrypoint.py` 的 `execvp` 之后不可达分支）。
- **B 截图默认归一化**：PhoNote 的截图默认压成 WebP（长边 1600、质量 82），`keep_original=true` 按次保留原图；Pillow 是可选的（没装就原样存，不报错）。
- 文档：README 补 PostgreSQL 用法、`/metrics` 鉴权、截图压缩、版本与发布口径与新的已知限制。

**关键决策与理由**：
- **PostgreSQL 复用 SQLite 的入参模型而不是新造一套**：工具契约的重点是「Agent 学一次就够」，两套形状会让模型每次都要判断后端；差异只留在插件内部（只读会话、`%s` 占位符）。
- **连接超时必须显式设**：实测不设 `connect_timeout` 时，DSN 指向一个不响应的端口会让 `psycopg.connect` 一直挂着，把平台启动拖死——插件装载失败要好过启动卡住。
- **远程插件只对「连接层失败」重试**：幂等不是靠重试次数保证的，而是靠「请求有没有可能已经到达」判断；读超时重试等于给写操作加了一次执行机会。
- **镜像默认非 root + 只读根文件系统**：这是阶段 7「最接近生产的两条限制」之一；代价就是必须显式声明 `/data` 卷（否则连演示库都种不出来），这个代价换掉的是「容器被攻破后还能改自己的文件系统」。
- **`/metrics` 鉴权做成可选开关而不是默认开**：默认开会让现有的 compose/Prometheus 抓取全部 401，属于破坏性变更；开关默认关、文档写清「暴露到不可信网络就开」。
- **版本号升到 0.1.0 但不推镜像仓库**：发布物是 tag + Release + 可复现的 `docker compose build`；推镜像仓库会引入一套凭据与清理策略，当前收益不足。

**验收证据**（本机全量）：
- `ruff check .` → All checks passed；`ruff format --check src tests deploy examples` → **77 files already formatted**；`mypy` → Success: no issues found in **47 source files**；`pytest --cov --cov-fail-under=90` → exit 0，**375 passed / 0 failed**，覆盖率 **94.75%**（2668 语句 / 140 未覆盖）。
- PostgreSQL 真实集成：`docker run ... postgres:16-alpine`（端口 55432 → 5432）+ `DRIVER_TEST_POSTGRES_DSN=postgresql://demo:secret@127.0.0.1:55432/shop` 跑通 `search_postgres` 的过滤、分页与未知表/未知列报错；没设该环境变量时相关用例 `pytest.skip`。
- 容器加固实测：`docker run --read-only --tmpfs /tmp --security-opt no-new-privileges:true --memory 512m --cpus 1 driver-platform:0.1.0` → `Up (healthy)`；`id` → `uid=10001(driver)`；`/data/demo.db` 属主 `driver`；`open('/app/evil.txt','w')` → `OSError: [Errno 30] Read-only file system`；`search_database` 经 HTTP 返回上海两行；`DRIVER_METRICS_PROTECTED=1` 时 `/metrics` 无钥匙 401、带钥匙 200。
- 远程插件：MockTransport 下清单缓存命中（两次 `fetch_manifest()` 只 1 次请求）、连接失败重试 1 次、`ReadTimeout` 不重试。
- 截图归一化：Pillow 13.0.0 下默认落盘 `image/webp` 且以 `RIFF`+`WEBP` 开头，`keep_original=true` 时字节等于原 PNG。
- Electron 壳真启：`npm install`（经 `ELECTRON_MIRROR` 镜像，npm 11 的 allow-scripts 默认拦住 postinstall，需手动 `node node_modules/electron/install.js`）→ `electron.exe .` 启动，窗口标题 `PhoNote`、四分区渲染正常、`/notes/api/tree` 返回空树、无 stdout/stderr 报错。

**已知限制**：PostgreSQL 插件无事务/连接池/SSL 参数可调，`connect_timeout` 固定 5 秒；`/metrics` 默认仍公开；Pillow 缺失时图片原样落盘；Electron 的全局热键（`Ctrl+Shift+S`）在本次真启中未实际按键验证（只验证了窗口渲染与 REST 通路）；未加 3.11/3.12 的 CI 矩阵。

---

## 非阶段交付：收口五项——编排与分发（2026-11-13）

### 目标

把「只在开发机上成立」的五件事做成可验证的交付物：低版本兼容、真编排层、镜像体积、镜像分发、跨进程链路。

### 交付物

1. **CI 矩阵**（`.github/workflows/ci.yml`）：`test` 任务跑 `3.11 / 3.12 / 3.14` 三档；静态检查、覆盖率门禁、
   bandit 与 pip-audit 只在 3.14 上跑一遍（重复三遍只是耗额度）。
2. **K8s 清单**（`deploy/k8s/`）：`namespace / secret.example / deployment / service / hpa / pdb / kustomization`，
   `kubectl apply -k deploy/k8s` 一次起全套；3 副本、`maxUnavailable: 0`、只读根文件系统、非 root、drop ALL、
   `/health` 探针、`emptyDir` 数据卷、CPU 70% 的 HPA、`minAvailable: 2` 的 PDB。
3. **镜像瘦身**（`Dockerfile`）：多阶段构建 + `python:3.14-alpine`，venv 独立安装、删 pip/setuptools/wheel 与 `.pyc`；
   镜像从 **245 MB 降到 118 MB**（本机 `docker images` 实测）。
4. **GHCR 发布**（`publish` 任务）：仅 master、`permissions: packages: write`，推 `:<版本>` 与 `:sha-<7位>` 两个 tag。
5. **W3C traceparent 跨进程**：`observability/context.py` 解析/生成 `traceparent`，ASGI 中间件接着上游 trace 换本进程 span
   并在响应头回写；日志带 `trace_id`；出网点（远程插件、AI 调用、WebDAV 同步）用 `outbound_trace_headers()` 带给下游。
6. **Electron 热键实测**：真启 Electron，`AppActivate` + `SendKeys "^+s"`，界面上出现「已截图: cap_18e690be0d2d4247」、
   图片区「剩 1 张」，`GET /notes/api/captures` 读到 `image/webp 1600 x 900 107230 bytes source=shell`。

### 关键决策与理由

- **矩阵只跑测试**：ruff/mypy/pytest+覆盖率在 3.14 上一次就够；3.11/3.12 的增量信息是「代码能不能 import、测试过不过」。
- **K8s 与 Compose 并存**：Compose 保留为「一台机器一条命令」的对照物，K8s 是交给编排层的形态；两者共用同一个镜像。
- **K8s 数据卷用 emptyDir**：SQLite 不支持多副本共享写，演示库每副本一份；真要共享数据应开 PostgreSQL 插件（已落地）。
- **换 alpine 换掉 glibc**：本项目依赖全是纯 Python + 有 musllinux 轮子的扩展（uvloop/httptools）；换来 127 MB 的体积差，
  代价是「与 Debian 上的行为差异需自行留意」，写进已知限制。可选的 psycopg / Pillow 不进镜像。
- **不把删除放在单独一层**：一开始在 slim 上「删标准库测试套件」没有任何效果（`docker images` 仍是 227 MB）——
  删除写在后续层只是加白洞，删不掉前面层里的字节；真正的体积来自换基础镜像。
- **traceparent 只做传播**：不引 OTel SDK、不做 span 树与采样；每个进程在响应头回写自己的 span，日志里带 `trace_id`，
  足够把「一次请求穿过网关 → 平台 → 远程插件/AI/WebDAV」串起来。

### 验收证据

- 门禁：`ruff check .` 通过、`ruff format --check src tests deploy examples` 77 文件、`mypy` 47 文件、
  `pytest --cov --cov-fail-under=90` 全绿（本机 3.14）。
- 镜像：`driver-platform:0.1.0` = **118 MB**（此前 245 MB）；`docker compose up -d` 三容器 healthy，
  `POST /v1/tools/execute`（`read-key`）返回上海两行；响应头带 `traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-128e7b3edda8405e-01`
  （上游 trace 保留、span 换新）。
- 热键：如上第 6 条，截图落成 `media/8a/b6/8ab647ce…webp`。
- GHCR：本机 `docker push ghcr.io/qw2007/driver-platform:0.1.0` 失败，原文
  `error from registry: permission_denied: The token provided does not match expected scopes.`
  （本机 token 只有 `gist, read:org, repo, workflow`）——推送因此改为由 CI 的 `GITHUB_TOKEN` 完成。
- K8s：**没有集群可跑**（Docker Desktop 的内置 Kubernetes 在 7 分钟内始终没有生成 kubeconfig；
  `kind` 的二进制从 GitHub Releases 下载返回 HTTP 000；`kubectl apply --dry-run=client` 也需要 API server 做 discovery）。
  因此只做到结构静态校验：`tests/test_k8s_manifests.py` 7 个用例守住副本数/策略/探针/securityContext/Secret 引用/
  镜像 tag 与包版本一致/Service 与 PDB 的 selector 一致。**清单未在真集群上 apply 过**。

### 已知限制

- 3.11/3.12 的结论只来自 CI，本机无法对照复现。
- K8s 清单未经真集群验证；HPA 依赖 metrics-server（kind/minikube 默认不装）。
- alpine 镜像用 musl；若将来引入需要 glibc 的扩展（某些二进制轮子），需回到 slim 或换 musllinux 轮子。
- 镜像仍钉 amd64 摘要的第三方服务（Prometheus/Grafana）与标签基础镜像的取舍未变；GHCR 仓库为私有，拉取需 `imagePullSecrets`。
- traceparent 不采样、不导出 span；下游服务若忽略该头，链路只到「本进程 → 下游进程」这一跳。

## 当前状态（截至最新提交）

- **已完成**：阶段 0 / 1 / 2 / 3 / 4 / 5 / 6 / 7 / 8 —— 任务书的九阶段验收标准全部落地，外加 8 次文档提交、1 次工具链与环境提交、2 次任务状态更新，以及 5 个非阶段交付（PhoNote 插件 P0/P1/P2/P3 + 收口四项决策 + 收口八项能力补齐，见上文；换机器或换会话继续开发前先读 [`docs/handoff.md`](docs/handoff.md)）；
- **门禁（最近一次全量验证）**：`ruff check .` → All checks passed；`ruff format --check src tests deploy examples` → **79 files** already formatted；`mypy` → Success: no issues found in **47 source files**（已覆盖 `src` + `deploy` + `examples`）；`pytest --cov --cov-fail-under=90` → **391 个用例、0 失败**（3 个 PostgreSQL 集成用例本机无库跳过；CI 里带 `postgres` service 会真跑），覆盖率 **93.12%**（2719 语句 / 187 未覆盖）；CI 已把覆盖率门禁（90%）与 `bandit` / `pip-audit` 一起接进去；
- **包版本**：**`0.1.0`**（`pyproject.toml` 与 `src/driver_platform/__init__.py` 同源，compose 镜像标签跟随；对外契约版本仍走各工具的 `contract_version`，与包版本解耦）；发布物是 git tag `v0.1.0` + GitHub Release + CI 推到 GHCR 的镜像（`:0.1.0` 与 `:sha-<7位>`）；
- **下一步**：九阶段目标与四轮收口全部落地（详见上文各节）。仍未验证的一件事是 **K8s 清单没有在真集群上 apply 过**（本机 Docker Desktop 的内置集群起不来、`kind` 也下载不到）；除此之外的待办都是「需要外部资源或收益不足」：镜像推私有仓库的拉取凭据（`imagePullSecrets`）、HPA 依赖的 metrics-server、3.11/3.12 的本机复现。

## 已知限制（汇总）

1. CI 以 Python 3.14 为准，`requires-python` 放宽到 3.11+，但 3.11 / 3.12 未实测；
2. 工具执行没有超时与并发限制，详见阶段 1 说明；
3. SQLite 插件单连接串行、`total` 多一次 `COUNT(*)`，详见阶段 2 说明；
4. MCP 侧只做 stdio 与 tools，协议版本跟随 SDK，详见阶段 3 说明；
5. 鉴权默认关且只到工具名粒度，无速率限制/配额/审计/吊销，详见阶段 4 说明；
6. 指标默认关、`/metrics` 无鉴权且是**进程内**的（重启归零），`request_id` 只在本进程内贯穿（无 traceparent），访问日志不记请求体，详见阶段 5 说明；
7. 远程插件默认关、清单缓存 60 秒且只在装载/重连时拉、连接失败重试一次（读超时与 5xx 不重试）、无熔断/签名校验，`base_url` 是单点且每次调用新建 HTTP 客户端，详见阶段 6 说明；
8. 部署的默认形态是**单副本**（重启期间不可用）；容器已改成非 root（uid 10001）+ 只读根文件系统 + `no-new-privileges` + 512m/1 CPU 限额，但镜像仍 241 MB、第三方镜像钉的是 amd64 摘要而基础镜像用标签，compose 里的演示钥匙是明文默认值（对外部署必须用 `.env` 覆盖），`/metrics` 默认无鉴权（可开 `DRIVER_METRICS_PROTECTED=1`）且暴露在宿主端口，Grafana 自带插件不自更新（升级只能改镜像摘要），详见阶段 7 说明；
9. 多副本是**单机 Compose 的 `--scale`**、不是编排（不自动补副本、无滚动策略、跨宿主无从谈起），nginx 解析有 5 秒窗口，重试对非幂等请求也开着，多副本共享同一份只读数据卷，探针只衡量成功率，`/metrics` 经网关暴露且仍无鉴权，远程插件的 HTTP 客户端会继承宿主机代理设置，详见阶段 8 说明；
10. **本机开发环境的沙箱限制（不影响 CI）**：受限模式下 `pip` 安装、全量 pytest、`docker` 命令都必须提权 —— pytest 用 `Path.mkdir(mode=0o700)` 建临时目录，该目录的 DACL 里 `BUILTIN\Administrators` 对受限令牌是 deny-only，于是**创建者自己都进不去**（`PermissionError [WinError 5]`，本机 30 个用例受影响）；Docker CLI 要开命名管道，受限进程不被允许；本机 DSH 会话里 **Python 子进程**连 `danger-full-access` 都解不开（写不了工作区外、打不开 Docker 命名管道），所以本机滚动验收用等价 PowerShell 序列替代 `deploy/rollout.py`（见阶段 8 说明的验收证据第 5 节）。逐条解法（含 GitHub 直连、镜像加速、提权清单、`0o700` 取证）见 [`docs/environment.md`](docs/environment.md)。
11. **PhoNote 插件（P0/P1/P2/P3）**：单机单用户（SQLite + 本地文件）；同步是单主设备、不做双向合并，只实现 `MKCOL`/`PUT`/`GET`/`DELETE` 四个动词、没有断点续传，远端只增不删媒体，真实云盘（坚果云/Nextcloud）未连过；批注锚点里的选中文字/图片区域坐标仍是占位 JSON；图片只能按 sha256 从磁盘读、无缩略图；导出是「复制 Markdown + 绝对路径」，不是打包下载；删除不级联删图（图留在缓冲区）；AI 只有 openai/anthropic/manual 三档、无重试与流式、每次最多 6 张图；多屏只截第一屏；截图压缩依赖 Pillow（缺省时装不上就原样存，体积不受控）。壳已在本机真启验证（窗口与四分区渲染正常、REST 通路可用），但全局热键 `Ctrl+Shift+S` 未实际按键验证；`npm install` 需 `ELECTRON_MIRROR` 镜像，且 npm 11 默认拦 postinstall，要手动跑 `node node_modules/electron/install.js`。
12. **PostgreSQL 插件**：只做只读等值查询（无事务控制、连接池、可调 SSL 参数），`connect_timeout` 固定 5 秒；库与列元数据走 `information_schema`（只认默认 schema），跨 schema 查询未支持。
