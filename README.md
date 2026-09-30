# Driver Platform（驱动平台）

连接 AI Agent（Codex / Claude / ChatGPT 等）与数据库等工具的**中间层**：工具总线 + 协议适配 + 插件宿主。不是业务系统。

## 阶段路线（每阶段独立验收）

| 阶段 | 目标 | 关键验收 | 状态 |
|---|---|---|---|
| 0 | 骨架 + CI | `GET /health` → ok；CI 跑 pytest + ruff | ✅ 已完成 |
| 1 | 核心服务 + 工具注册 | `execute` 统一入口；echo 工具；非法参数 → 422 | ✅ 本阶段 |
| 2 | 插件系统 + 数据库插件 | 工具列表出现 `search_database`；可注册/注销；结构化结果 | 待开始 |
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
```

## 目录结构

```
src/driver_platform/
  main.py               create_app()：装配注册表、错误处理、路由
  errors.py             PlatformError 体系 + 统一错误信封
  api/tools.py          GET /v1/tools、POST /v1/tools/execute
  tools/                Tool 契约、ToolRegistry、echo 内置工具
tests/                  注册表单测 + 接口契约测试
docs/stages/            每阶段设计说明（阶段 0、阶段 1）
.github/workflows/ci.yml  CI：ruff check + pytest（Python 3.14）
Dockerfile              运行镜像（阶段 7 才验证）
```

## 阶段设计说明

每阶段的设计说明单独成文（选型理由 / 模块设计 / 接口定义 / 关键代码 / 测试与部署 / 非目标），README 只留路线表与结论：

- [阶段 0：骨架 + CI](docs/stages/stage-0.md)
- [阶段 1：核心服务 + 工具注册](docs/stages/stage-1.md) —— 对外契约（错误码、请求/响应形状、版本化规则）在这里

## 已知限制

- `mypy` 未接入：阶段 1 的 `Tool` 契约（泛型入参/出参模型）已定，可接入静态类型检查。
- CI 以 Python 3.14 为准，`requires-python` 放宽到 3.11+，但 3.11/3.12 未实测。
- 工具执行无超时与并发限制，细节见阶段 1 说明的「已知限制」。
