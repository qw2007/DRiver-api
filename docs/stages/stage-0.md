# 阶段 0：骨架 + CI

> 目标：`GET /health` → ok；CI 跑 pytest + ruff。状态：已完成并提交（commit `9cea789`）。

**技术选型理由**：规范首选 Python + FastAPI。理由是该路线后面几个阶段全靠生态——MCP SDK、Pydantic 校验（阶段 1 的 422 验收直接由它给出）、pytest/Testcontainers（阶段 2 的数据库插件集成测试）。Node/Go/Rust 备选只在「性能或单二进制部署是硬需求」时才值得付生态成本，当前不成立。

**模块设计**：阶段 0 只有「进程内 HTTP 服务」这一层，`create_app()` 工厂返回 FastAPI 实例，`app` 是 uvicorn 的默认入口。插件宿主、工具注册表、适配器都是后续阶段的事，此时不预留空目录、不做抽象。

**接口定义**：`GET /health` → `200 {"status": "ok"}`，无鉴权、无依赖（阶段 5 起这里加指标，阶段 4 起加鉴权）。

**测试与部署方案**：pytest 走 `TestClient`，对 `/health` 断言状态码与响应体；CI 在 Python 3.14 上 `ruff check` + `pytest`；镜像用 `python:3.14-slim` + `uvicorn`，本机无 Docker，Dockerfile 未实测，阶段 7 才纳入验收。

**已知限制**：CI 只跑 Python 3.14（`requires-python` 放宽到 3.11+，3.11/3.12 未实测）；Dockerfile 未实测。
