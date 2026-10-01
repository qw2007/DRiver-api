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
| 文档（任务书 + 变更日志） | `783b5c6` | 2026-10-01 | 4 files, +409 / −2 | `docs/task-brief.md` + `CHANGELOG.md` + README 入口 |
| — 工具链与环境约束 | 见文末 | 2026-10-01 | 6 files, +268 / −10 | 本地门禁全绿；沙箱三坑固化解法 |

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

## 当前状态（截至最新提交）

- **已完成**：阶段 0 / 1 / 2 / 3 / 4 / 5 / 6 / 7，外加 6 次文档提交、1 次工具链与环境提交与 1 次任务状态更新，共 16 个提交、82 个受版本控制的文件；
- **门禁（最近一次全量验证）**：`ruff check .` → All checks passed；`ruff format --check src tests deploy examples` → 58 files already formatted；`mypy src` → Success: no issues found in 34 source files；`pytest` → **256 passed**，覆盖率 **95%**（1248 语句 / 68 未覆盖；`bandit -r src -q` / `pip-audit` 在阶段 5 的工具链接入后未再单独复跑）；
- **包版本**：`pyproject.toml` 仍是 `0.0.0`，尚未随阶段递增（对外契约版本走各工具的 `contract_version`，与包版本解耦）；
- **下一步**：阶段 8「生产加固」—— 多副本、滚动更新不中断（compose 已用 `exec` 让 uvicorn 成为 PID 1，`SIGTERM` 直达应用，可直接接）、旧版 Agent 兼容；阶段 7 起全平台只需在仓库根执行 `docker compose up -d --build`。

## 已知限制（汇总）

1. CI 以 Python 3.14 为准，`requires-python` 放宽到 3.11+，但 3.11 / 3.12 未实测；
2. 工具执行没有超时与并发限制，详见阶段 1 说明；
3. SQLite 插件单连接串行、`total` 多一次 `COUNT(*)`，详见阶段 2 说明；
4. MCP 侧只做 stdio 与 tools，协议版本跟随 SDK，详见阶段 3 说明；
5. 鉴权默认关且只到工具名粒度，无速率限制/配额/审计/吊销，详见阶段 4 说明；
6. 指标默认关、`/metrics` 无鉴权且是**进程内**的（重启归零），`request_id` 只在本进程内贯穿（无 traceparent），访问日志不记请求体，详见阶段 5 说明；
7. 远程插件默认关、清单只在装载时拉一次、无重试/熔断/签名校验，`base_url` 是单点且每次调用新建 HTTP 客户端，详见阶段 6 说明；
8. 部署是**单副本**（重启期间不可用），容器以 root 运行、无资源限额与只读根文件系统，第三方镜像钉的是 amd64 摘要而基础镜像用标签，compose 里的演示钥匙是明文，`/metrics` 无鉴权且暴露在宿主端口，Grafana 自带插件不自更新（升级只能改镜像摘要），详见阶段 7 说明；
9. **本机开发环境的沙箱限制（不影响 CI）**：受限模式下 `pip` 安装、全量 pytest、`docker` 命令都必须提权 —— pytest 用 `Path.mkdir(mode=0o700)` 建临时目录，该目录的 DACL 里 `BUILTIN\Administrators` 对受限令牌是 deny-only，于是**创建者自己都进不去**（`PermissionError [WinError 5]`，本机 30 个用例受影响）；Docker CLI 要开命名管道，受限进程不被允许。逐条解法（含 GitHub 直连、镜像加速、提权清单、`0o700` 取证）见 [`docs/environment.md`](docs/environment.md)。
