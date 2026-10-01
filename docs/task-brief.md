# 任务书：驱动平台（Driver Platform）

> 本文件是项目的**整体任务目标**，随仓库一起发布。每阶段的落地细节见 `docs/stages/`，实现时间线见 [`CHANGELOG.md`](../CHANGELOG.md)。

## 一、定位

连接 AI Agent（Codex / Claude / ChatGPT 等）与数据库等工具的**中间层**：工具总线 + 协议适配 + 插件宿主。

- **不是业务系统**：不碰业务数据模型，也不替业务做判断；
- **不做 Agent 本身**，也不调用模型：平台只负责「有哪些工具、怎么调、谁可以调」；
- 一个**演进式原型**：从最小闭环开始，每个阶段都能独立验收，先跑通再加厚。

## 二、目标

让任何支持工具调用的 Agent，用**同一套接口**发现并调用平台上的工具；协议差异（MCP / OpenAI / 未来的 LangChain 等）由适配器吸收，互不依赖；工具以插件形式可插拔、可版本化、可下线。

一句话：Agent 只认平台，平台替 Agent 屏蔽「工具从哪来、用什么协议说、有没有权限」。

## 三、阶段路线与验收标准

| 阶段 | 目标 | 验收标准 | 状态 |
|---|---|---|---|
| 0 | 骨架 + CI | `GET /health` 返回 ok；CI 跑 pytest + ruff | ✅ 已完成 |
| 1 | 核心服务 + 工具注册 | `execute` 统一入口；echo 工具；非法参数 → 422 | ✅ 已完成 |
| 2 | 插件系统 + 数据库插件 | 工具列表出现 `search_database`；插件可注册/注销；返回结构化结果 | ✅ 已完成 |
| 3 | 协议适配层 | MCP 客户端能 list/call `search_database`；curl 返回 OpenAI 工具 JSON Schema | ✅ 已完成 |
| 4 | 安全权限 | 无凭证被拒绝；越权被拦 | ✅ 已完成 |
| 5 | 可观测性 | Grafana 看 QPS/延迟/错误率；日志可按 request_id 追全链路 | ✅ 已完成 |
| 6 | 远程插件 | 远程注册可调用；插件下线后工具自动不可用 | 待开始 |
| 7 | 部署交付 | `docker compose up` 起全平台；CI/CD 通过 | 待开始 |
| 8 | 生产加固 | 多副本；滚动更新不中断；旧版 Agent 兼容 | 待开始 |

## 四、八条核心原则（约束，不是建议）

1. **接口先行**：先定错误信封、请求/响应形状、版本化规则，再写实现；
2. **插件契约版本化**：破坏性变更必须升主版本（`contract_version`）；
3. **适配器隔离**：MCP / OpenAI / LangChain 适配器互不依赖，核心不依赖任何具体适配器；
4. **配置外部化**：可配项只从环境变量读，代码里不留第二份真相；
5. **测试从阶段 0 开始**：每个阶段都带测试，CI 是准入门槛；
6. **可观测性内建**：不是事后补的中间件；
7. **文档随代码同步**：README 路线表 + 每阶段设计说明；
8. **特性开关默认关**：插件、协议、鉴权默认全关，要用必须显式打开。

## 五、技术栈与外部依赖

| 类别 | 选择 | 说明 |
|---|---|---|
| 语言 / 框架 | Python 3.11+（CI 与实测为 3.14）、FastAPI、pydantic v2、uvicorn | 规范首选方案；MCP / Pydantic / pytest 生态必须 |
| 质量门禁 | ruff（lint + format）、mypy、pytest、httpx/TestClient | 四道门禁全绿才算阶段完成 |
| 协议 | 官方 `mcp` SDK（**可选依赖** `driver-platform[mcp]`）；OpenAI 侧只做纯 Schema 翻译 | 协议细节交给 SDK，平台只写「注册表 ↔ 协议类型」的翻译 |
| 数据库插件 | 先 SQLite（零依赖、可进 CI），PostgreSQL 复用同一插件契约 | PostgreSQL 留到阶段 7（Docker/Testcontainers） |

**本机环境的如实记录**：开发机上没有 Docker（阶段 7 的 `docker compose` 无法在本机验证），也没有 uv；受限沙箱下跑带 `tmp_path` 的 pytest 需要提权（原因见 `CHANGELOG.md` 的「已知限制」）。

## 六、交付规范

- 每阶段**先出设计说明，再落地代码**，设计说明含五件套：技术选型理由 / 模块设计 / 接口定义 / 关键代码示例 / 测试与部署方案 → `docs/stages/stage-N.md`；
- 每阶段结束必须 `ruff check .`、`ruff format --check src tests`、`mypy`、`pytest` 全绿，且 CI 可跑；
- 配置写错**启动即失败**，不静默降级；
- README 与阶段文档随代码同步更新。

## 七、发布方式

- GitHub 独立项目：**`driver-api-v0.1`**；
- 单一分支：**`master`**（不保留其他分支）。
