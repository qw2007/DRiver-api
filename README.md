# Driver Platform（驱动平台）

连接 AI Agent（Codex / Claude / ChatGPT 等）与数据库等工具的**中间层**：工具总线 + 协议适配 + 插件宿主。不是业务系统。

## 阶段路线（每阶段独立验收）

| 阶段 | 目标 | 关键验收 | 状态 |
|---|---|---|---|
| 0 | 骨架 + CI | `GET /health` → ok；CI 跑 pytest + ruff | ✅ 已完成 |
| 1 | 核心服务 + 工具注册 | `execute` 统一入口；echo 工具；非法参数 → 422 | ✅ 已完成 |
| 2 | 插件系统 + 数据库插件 | 工具列表出现 `search_database`；可注册/注销；结构化结果 | ✅ 本阶段 |
| 3 | 协议适配层 | MCP 客户端能 list/call `search_database`；curl 返回 OpenAI 工具 JSON Schema | 待开始 |
| 4 | 安全权限 | 无凭证拒绝；越权被拦 | 待开始 |
| 5 | 可观测性 | Grafana 看 QPS/延迟/错误率；日志可按 request_id 追全链路 | 待开始 |
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
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check src tests
.venv/Scripts/python -m mypy
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

## 目录结构

```
src/driver_platform/
  main.py               create_app()：装配配置、注册表、插件宿主、错误处理、路由
  config.py             Settings.from_env()：DRIVER_PLUGINS / DRIVER_SQLITE_PATH
  bootstrap.py          create_registry_and_host()：内置工具 + 按配置装载插件
  errors.py             PlatformError 体系 + 统一错误信封
  api/tools.py          GET /v1/tools、POST /v1/tools/execute
  api/plugins.py        GET /v1/plugins
  tools/                Tool 契约、ToolRegistry、echo 内置工具
  plugins/              Plugin 契约、PluginHost、插件目录、sqlite 插件
tests/                  注册表/接口契约/插件宿主/配置/search_database 测试
docs/stages/            每阶段设计说明（阶段 0、阶段 1、阶段 2）
.github/workflows/ci.yml  CI：ruff check + ruff format --check + mypy + pytest（Python 3.14）
Dockerfile              运行镜像（阶段 7 才验证）
```

## 阶段设计说明

每阶段的设计说明单独成文（选型理由 / 模块设计 / 接口定义 / 关键代码 / 测试与部署 / 非目标），README 只留路线表与结论：

- [阶段 0：骨架 + CI](docs/stages/stage-0.md)
- [阶段 1：核心服务 + 工具注册](docs/stages/stage-1.md) —— 对外契约（错误码、请求/响应形状、版本化规则）在这里
- [阶段 2：插件系统 + 数据库插件](docs/stages/stage-2.md) —— 插件契约、`search_database` 参数语义、配置与开关

## 已知限制

- CI 以 Python 3.14 为准，`requires-python` 放宽到 3.11+，但 3.11/3.12 未实测。
- 工具执行无超时与并发限制，细节见阶段 1 说明的「已知限制」。
- SQLite 插件单连接串行、`total` 多一次 `COUNT(*)`，细节见阶段 2 说明的「已知限制」。
