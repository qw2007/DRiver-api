# Driver Platform（驱动平台）

连接 AI Agent（Codex / Claude / ChatGPT 等）与数据库等工具的**中间层**：工具总线 + 协议适配 + 插件宿主。不是业务系统。

## 阶段路线（每阶段独立验收）

| 阶段 | 目标 | 关键验收 | 状态 |
|---|---|---|---|
| 0 | 骨架 + CI | `GET /health` → ok；CI 跑 pytest + ruff | ✅ 本阶段 |
| 1 | 核心服务 + 工具注册 | `execute` 统一入口；echo 工具；非法参数 → 422 | 待开始 |
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
```

检查：

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
```

## 目录结构

```
src/driver_platform/main.py   create_app()：FastAPI 实例与路由
tests/test_health.py          /health 契约测试
.github/workflows/ci.yml      CI：ruff check + pytest（Python 3.14）
Dockerfile                    运行镜像（阶段 7 才验证）
```

## 阶段 0 说明

**技术选型理由**：规范首选 Python + FastAPI。理由是该路线后面几个阶段全靠生态——MCP SDK、Pydantic 校验（阶段 1 的 422 验收直接由它给出）、pytest/Testcontainers（阶段 2 的数据库插件集成测试）。Node/Go/Rust 备选只在「性能或单二进制部署是硬需求」时才值得付生态成本，当前不成立。

**模块设计**：阶段 0 只有「进程内 HTTP 服务」这一层，`create_app()` 工厂返回 FastAPI 实例，`app` 是 uvicorn 的默认入口。插件宿主、工具注册表、适配器都是后续阶段的事，此时不预留空目录、不做抽象。

**接口定义**：`GET /health` → `200 {"status": "ok"}`，无鉴权、无依赖（阶段 5 起这里加指标，阶段 4 起加鉴权）。

**测试与部署方案**：pytest 走 `TestClient`，对 `/health` 断言状态码与响应体；CI 在 Python 3.14 上 `ruff check` + `pytest`；镜像用 `python:3.14-slim` + `uvicorn`，本机无 Docker，Dockerfile 未实测，阶段 7 才纳入验收。

## 已知限制

- `mypy` 未接入：ruff 已覆盖 lint 与格式，类型检查在契约（阶段 1 的 `BasePlugin`）定下来后再加，避免现在对着空接口调类型。
- CI 以 Python 3.14 为准，`requires-python` 放宽到 3.11+，但 3.11/3.12 未实测。
