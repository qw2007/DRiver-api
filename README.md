# Driver Platform（驱动平台）

连接 AI Agent（Codex / Claude / ChatGPT 等）与数据库等工具的**中间层**：工具总线 + 协议适配 + 插件宿主。不是业务系统。

## 阶段路线（每阶段独立验收）

| 阶段 | 目标 | 关键验收 | 状态 |
|---|---|---|---|
| 0 | 骨架 + CI | `GET /health` → ok；CI 跑 pytest + ruff | ✅ 已完成 |
| 1 | 核心服务 + 工具注册 | `execute` 统一入口；echo 工具；非法参数 → 422 | ✅ 已完成 |
| 2 | 插件系统 + 数据库插件 | 工具列表出现 `search_database`；可注册/注销；结构化结果 | ✅ 已完成 |
| 3 | 协议适配层 | MCP 客户端能 list/call `search_database`；curl 返回 OpenAI 工具 JSON Schema | ✅ 已完成 |
| 4 | 安全权限 | 无凭证拒绝；越权被拦 | ✅ 已完成 |
| 5 | 可观测性 | Grafana 看 QPS/延迟/错误率；日志可按 request_id 追全链路 | ✅ 已完成 |
| 6 | 远程插件 | 远程注册可调用；下线后工具自动不可用 | 待开始 |
| 7 | 部署交付 | `docker compose up` 起全平台；CI/CD 通过 | 待开始 |
| 8 | 生产加固 | 多副本；滚动更新不中断；旧版 Agent 兼容 | 待开始 |

## 八条核心原则（约束，不是建议）

接口先行 → 插件契约版本化（破坏性变更升主版本）→ 适配器隔离（MCP / OpenAI / LangChain 互不依赖）→ 配置外部化（全走环境变量）→ 测试从阶段 0 开始 → 可观测性内建 → 文档随代码同步 → 特性开关默认关。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows；Linux/macOS 用 .venv/bin/python
.venv/Scripts/python -m uvicorn driver_platform.main:app --port 8000
curl http://127.0.0.1:8000/health                  # {"status":"ok"}
curl http://127.0.0.1:8000/v1/tools                # 已注册工具及其契约（JSON Schema）
curl -X POST http://127.0.0.1:8000/v1/tools/execute -H "Content-Type: application/json" \
     -d '{"tool":"echo","arguments":{"message":"hi","times":2}}'
```

检查：

```bash
.venv/Scripts/python -m pytest                      # 本机沙箱下需提权，原因见 docs/environment.md
.venv/Scripts/python -m pytest --cov=driver_platform --cov-report=term-missing
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check src tests
.venv/Scripts/python -m mypy
.venv/Scripts/python -m bandit -r src -q
.venv/Scripts/python -m pip_audit --cache-dir .pip-audit-cache --progress-spinner off
```

### 带数据库插件启动（阶段 2）

插件默认不装（特性开关默认关），要靠环境变量显式开启；启用了 `sqlite` 却没给路径、或文件不存在，**启动即失败**：

```bash
DRIVER_PLUGINS=sqlite DRIVER_SQLITE_PATH=/tmp/demo.db \
  .venv/Scripts/python -m uvicorn driver_platform.main:app --port 8000
curl http://127.0.0.1:8000/v1/plugins                # [{"plugin_id":"sqlite",...}]
curl -X POST http://127.0.0.1:8000/v1/tools/execute -H "Content-Type: application/json" \
     -d '{"tool":"search_database","arguments":{"table":"drivers","columns":["id","name"],"limit":2}}'
```

### 接协议（阶段 3）

协议也是**默认关**：`DRIVER_PROTOCOLS` 留空则全关，写了不支持的协议名启动即失败。

**导出给 OpenAI 兼容客户端**（纯 Schema 翻译，不调用模型）：

```bash
DRIVER_PROTOCOLS=openai .venv/Scripts/python -m uvicorn driver_platform.main:app --port 8000
curl http://127.0.0.1:8000/v1/protocols/openai/tools   # {"tools":[{"type":"function","function":{...}}]}
```

**作为 MCP 服务端接给本地 Agent**（stdio；`mcp` 是可选依赖，装 `pip install -e ".[mcp]"`）。
Claude Desktop / Codex 的 `mcpServers` 配置：

```json
{"mcpServers": {"driver-platform": {
  "command": "<项目路径>/.venv/Scripts/python.exe",
  "args": ["-m", "driver_platform.protocols.mcp_server"],
  "env": {"DRIVER_PROTOCOLS": "mcp", "DRIVER_PLUGINS": "sqlite", "DRIVER_SQLITE_PATH": "/tmp/demo.db"}}}}
```

stdout 是协议通道，诊断一律写 stderr；工具执行失败按 MCP 惯例回 `isError=true` 的结果（错误码与 HTTP 侧相同），不是协议错误。

### 开凭证与作用域（阶段 4）

鉴权**默认关**（本地开发、stdio 由 Agent 自己 spawn，没有监听端口）；要开门就显式配：

```bash
DRIVER_AUTH_MODE=required DRIVER_API_KEYS="s3cret-full, s3cret-echo:echo" \
  .venv/Scripts/python -m uvicorn driver_platform.main:app --port 8000
curl -H "Authorization: Bearer s3cret-echo" http://127.0.0.1:8000/v1/tools      # 只有 echo
curl -H "X-API-Key: s3cret-full" http://127.0.0.1:8000/v1/plugins              # 全权，可见 sqlite
```

`secret:tool1|tool2` 是该钥匙的**允许列表**（`secret` 或 `secret:*` = 全部工具）；越权是 403 `permission_denied`，无凭证/坏凭证是 401，`/health` 永远公开。MCP 一侧把**自己那一把**钥匙放进 `"env": {"DRIVER_PROTOCOLS": "mcp", "DRIVER_API_KEY": "s3cret-echo"}`（服务端清单是复数，客户端单数）。鉴权关闭时启动日志会警告，且**带了凭证就必须有效**。

### 开可观测性（阶段 5）

指标**默认关**（多开一个无鉴权端点就是多一块暴露面），日志与 `X-Request-ID` 常开（零契约成本）：

```bash
DRIVER_METRICS_ENABLED=1 DRIVER_LOG_FORMAT=json DRIVER_LOG_LEVEL=INFO \
  .venv/Scripts/python -m uvicorn driver_platform.main:app --host 0.0.0.0 --port 8000
curl http://127.0.0.1:8000/metrics | head                 # 关掉开关时这里返回 404
curl -i -H "X-Request-ID: trace-1" http://127.0.0.1:8000/health | grep -i x-request-id
```

四个指标：`driver_http_requests_total{method,route,status}`、`driver_http_request_duration_seconds{method,route}`、`driver_tool_calls_total{tool,outcome}`、`driver_tool_duration_seconds{tool,outcome}`。`outcome` 与错误信封的 `code` 是**同一套词汇**（`success` / `permission_denied` / `invalid_arguments` / `tool_not_found` / `tool_execution_failed` / `internal_error`）；`route` 只取**路由模板**、未知工具名统一进 `unknown`——两者都是为了不让调用方的输入撑爆时间序列基数。工具执行的埋点只有一处（`tools/executor.py`），所以 HTTP、MCP、以后接的远程插件自动同源。

本机看板（Prometheus 9090 + Grafana 3000，配置在 `docs/observability/`）：

```bash
docker run -d --name driver-prom -p 9090:9090 \
  -v "$PWD/docs/observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro" prom/prometheus
docker run -d --name driver-grafana -p 3000:3000 grafana/grafana
# 浏览器导入 docs/observability/grafana-dashboard.json，选刚加的 Prometheus 数据源
```

容器里的 Prometheus 抓的是 `host.docker.internal:8000`，所以这个场景下 uvicorn 必须绑 `0.0.0.0`；绑 `127.0.0.1` 时容器侧连不上（本机实测踩过）。

## 目录结构

```
src/driver_platform/
  main.py               create_app()：装配配置、注册表、插件宿主、错误处理、路由
  config.py             Settings.from_env()：插件 / SQLite 路径 / 协议 / 鉴权模式 / 钥匙清单
  bootstrap.py          create_registry_and_host()：内置工具 + 按配置装载插件
  errors.py             PlatformError 体系 + 统一错误信封
  security/             凭证与作用域：keys（解析与主体）、policy（authenticate + 可见性）、http（请求头 → 依赖）
  api/tools.py          GET /v1/tools、POST /v1/tools/execute
  api/plugins.py        GET /v1/plugins
  tools/                Tool 契约、ToolRegistry、executor（HTTP 与 MCP 共用执行路径）、echo 内置工具
  plugins/              Plugin 契约、PluginHost、插件目录、sqlite 插件
  protocols/            协议适配器：__init__（协议开关 + 路由装配）、openai_schema（纯翻译）、mcp（stdio 服务端）、mcp_server（进程入口）、routes
  observability/        request_id 上下文、四个指标、结构化日志（text/json）、ASGI 中间件、/metrics 路由
tests/                  注册表/接口契约/插件宿主/配置/search_database/协议适配/安全权限/可观测性 测试
.github/workflows/ci.yml  CI：ruff check + ruff format --check + mypy + pytest（Python 3.14）
Dockerfile              运行镜像（阶段 7 才验证）
docs/                   阶段设计说明（stages/）+ 任务书 + 变更日志 + 环境与工具手册
```

## 阶段设计说明

每阶段的设计说明单独成文（选型理由 / 模块设计 / 接口定义 / 关键代码 / 测试与部署 / 非目标），README 只留路线表与结论：

- [阶段 0：骨架 + CI](docs/stages/stage-0.md)
- [阶段 1：核心服务 + 工具注册](docs/stages/stage-1.md) —— 对外契约（错误码、请求/响应形状、版本化规则）在这里
- [阶段 2：插件系统 + 数据库插件](docs/stages/stage-2.md) —— 插件契约、`search_database` 参数语义、配置与开关
- [阶段 3：协议适配层](docs/stages/stage-3.md) —— MCP stdio 服务端、OpenAI Schema 导出、协议开关与错误映射
- [阶段 4：安全权限](docs/stages/stage-4.md) —— API Key 与工具作用域、401/403 语义、凭证来源（HTTP 头 / 环境变量）
- [阶段 5：可观测性](docs/stages/stage-5.md) —— 四个指标与标签取值约定、`/metrics` 的开关与暴露面、日志字段契约、request_id 的贯穿路径

项目级文档：

- [任务书：整体目标与 9 阶段验收标准](docs/task-brief.md)
- [变更日志：阶段 0–5 的逐阶段实现记录与验收证据](CHANGELOG.md)
- [环境与工具手册：本机工具链、提权清单、沙箱约束与 GitHub/Docker 直连解法](docs/environment.md)

## 已知限制

- CI 以 Python 3.14 为准，`requires-python` 放宽到 3.11+，但 3.11/3.12 未实测。
- 工具执行无超时与并发限制，细节见阶段 1 说明的「已知限制」。
- SQLite 插件单连接串行、`total` 多一次 `COUNT(*)`，细节见阶段 2 说明的「已知限制」。
- MCP 侧只做 stdio 与 tools（无 HTTP/SSE、无 resources/prompts、一次一个客户端），协议版本跟随 SDK，`output_model` 即协议输出契约，细节见阶段 3 说明的「已知限制」。
- 鉴权默认关且只到「工具名」这一层：不配 `DRIVER_AUTH_MODE=required` 就是开放的，密钥明文放在环境变量里，没有速率限制、配额、审计与吊销，细节见阶段 4 说明的「已知限制」。
- 指标默认关、`/metrics` 无鉴权且是**进程内**的（重启归零，多副本各算各的）；`X-Request-ID` 只在**本进程内**贯穿（没有 traceparent），日志不记请求体，直方图用默认桶（5 ms–10 s），细节见阶段 5 说明的「已知限制」。
- 本机（Windows + DSH 沙箱）有三处环境级坑：pip / 全量 pytest / docker / **联网 Git 操作**需提权，GitHub 需 `sslBackend=schannel` 直连，Docker 需镜像加速——**这些只是本机噪声，不影响 CI**，逐条解法见[环境与工具手册](docs/environment.md)。
