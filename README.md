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
| 6 | 远程插件 | 远程注册可调用；下线后工具自动不可用 | ✅ 已完成 |
| 7 | 部署交付 | `docker compose up` 起全平台；CI/CD 通过 | ✅ 已完成 |
| 8 | 生产加固 | 多副本；滚动更新不中断；旧版 Agent 兼容 | ✅ 已完成 |

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

`postgres` 插件是同一个工具形状的另一种后端（`search_postgres`，入参输出与 `search_database` 完全一致，只是后端换成 PostgreSQL 的只读会话）：

```bash
pip install 'driver-platform[postgres]'         # 可选依赖：psycopg[binary]
DRIVER_PLUGINS=postgres \
DRIVER_POSTGRES_DSN='postgresql://user:secret@db.internal:5432/shop' \
  .venv/Scripts/python -m uvicorn driver_platform.main:app --port 8000
curl -X POST http://127.0.0.1:8000/v1/tools/execute -H "Content-Type: application/json" \
     -d '{"tool":"search_postgres","arguments":{"table":"customers","filters":{"city":"上海"},"limit":2}}'
```

连接用 `options=-c default_transaction_read_only=on` 强制只读、`connect_timeout=5`（不设时连一个死端口会一直挂着，把平台启动拖住）。DSN 里的密码不会进日志：错误信息里只回 `host/database`。

如果 `postgres` 插件没写 DSN、或连不上，**启动即失败**（和 sqlite 同一套策略：宁可起不来，也不要带一个坏插件跑起来）。

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

`/metrics` 默认**不要求凭证**（抓它的是集群内的 Prometheus）。要把端口暴露到不可信网络，就再开一层：`DRIVER_METRICS_PROTECTED=1` 之后它走和 `/v1/tools` 同一套凭证——无钥匙 401，带一把有效钥匙 200；`DRIVER_AUTH_MODE=off` 时这个开关没有意义（没有凭证可校验）。

四个指标：`driver_http_requests_total{method,route,status}`、`driver_http_request_duration_seconds{method,route}`、`driver_tool_calls_total{tool,outcome}`、`driver_tool_duration_seconds{tool,outcome}`。`outcome` 与错误信封的 `code` 是**同一套词汇**（`success` / `permission_denied` / `invalid_arguments` / `tool_not_found` / `tool_execution_failed` / `internal_error`）；`route` 只取**路由模板**、未知工具名统一进 `unknown`——两者都是为了不让调用方的输入撑爆时间序列基数。工具执行的埋点只有一处（`tools/executor.py`），所以 HTTP、MCP、以后接的远程插件自动同源。

本机看板（Prometheus 9090 + Grafana 3000，配置在 `docs/observability/`）：

```bash
docker run -d --name driver-prom -p 9090:9090 \
  -v "$PWD/docs/observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro" prom/prometheus
docker run -d --name driver-grafana -p 3000:3000 grafana/grafana
# 浏览器导入 docs/observability/grafana-dashboard.json，选刚加的 Prometheus 数据源
```

容器里的 Prometheus 抓的是 `host.docker.internal:8000`，所以这个场景下 uvicorn 必须绑 `0.0.0.0`；绑 `127.0.0.1` 时容器侧连不上（本机实测踩过）。

### 接远程插件（阶段 6）

远端只要实现两个 HTTP 端点（契约见[阶段 6 说明](docs/stages/stage-6.md)），就能把自己的工具挂到平台上来；管理接口**默认关**，且与工具调用**分权**（`plugin_admin` 不是工具名）：

```bash
.venv/Scripts/python examples/remote_echo_service.py --port 9100 &   # 仓库自带的示例远端

DRIVER_REMOTE_PLUGIN_ENABLED=1 DRIVER_AUTH_MODE=required \
DRIVER_API_KEYS='admin-key:plugin_admin,all-key' \
  .venv/Scripts/python -m uvicorn driver_platform.main:app --host 0.0.0.0 --port 8000

# 注册 → 调用 → 下线（下线后工具立刻从 /v1/tools 消失、调用变 404）
curl -X POST http://127.0.0.1:8000/v1/plugins/remote -H "X-API-Key: admin-key" \
  -H "content-type: application/json" -d '{"plugin_id":"echo_farm","base_url":"http://127.0.0.1:9100"}'
curl -X POST http://127.0.0.1:8000/v1/tools/execute -H "X-API-Key: all-key" \
  -H "content-type: application/json" -d '{"tool":"remote_echo","arguments":{"message":"hi"}}'
curl -X DELETE http://127.0.0.1:8000/v1/plugins/remote/echo_farm -H "X-API-Key: admin-key" -i
```

`DRIVER_REMOTE_PLUGINS='id=base_url'` 可以在**启动期**装载远程插件；此时远端连不上进程就起不来——这是刻意的（宁可起不来，也不要带着一半坏工具对外服务），代价是部署编排里必须安排启动顺序。

### 接笔记插件 pho_note（PhoNote P0/P1/P2/P3）

`pho_note` 是 PhoNote 桌面应用的领域插件：截图进缓冲区 → 任意多图组成笔记 → 读回/改/批注/版本 → AI 总结、讨论与增量补丁 → 备份与 WebDAV 同步。它只依赖平台契约，UI 与全局热键在[独立仓库](https://github.com/qw2007/PhoNote)的 Electron 壳里。

```bash
DRIVER_PLUGINS=pho_note DRIVER_NOTES_DIR="$HOME/PhoNote" DRIVER_NOTES_UI=1 \
DRIVER_NOTES_LLM=manual \
  .venv/Scripts/python -m uvicorn driver_platform.main:app --host 127.0.0.1 --port 8000

curl http://127.0.0.1:8000/v1/tools        # echo + 二十六个 note_* 工具
curl http://127.0.0.1:8000/notes/api/tree  # 给壳用的 REST 层（DRIVER_NOTES_UI=1 才挂）
```

二十六个工具：树（`note_tree`/`note_node_create`/`note_node_rename`/`note_node_move`/`note_node_delete`）、截图缓冲（`note_add_capture`/`note_list_captures`）、笔记（`note_compose`/`note_get`/`note_list`/`note_update`/`note_set_images`）、批注（`note_annotate`/`note_annotate_delete`）、版本（`note_revisions`/`note_rollback`）、导出（`note_export`）、AI（`note_summarize`/`note_chat`/`note_messages`/`note_apply_patch`/`note_ingest_ai_reply`）、备份与同步（`note_backup`/`note_sync_push`/`note_sync_pull`/`note_sync_status`）。

数据落在 `DRIVER_NOTES_DIR`（默认 `~/PhoNote`）：`phonote.db`（自由文件树 + 图片元数据 + 批注 + 版本 + 消息）+ `media/ab/cd/<sha256>.<ext>`（内容寻址，同一张图只存一份）。

**截图默认压成 WebP**（长边 1600、质量 82）：一个学期的课存原尺寸会变成几百 MB。要留原图就按次传 `keep_original=true`（`note_add_capture` 与 `POST /notes/api/captures` 都支持），这时字节原样落盘、`media_type` 保持原格式。压缩需要可选依赖 Pillow（`pip install 'driver-platform[images]'`）；没装也能用，图片按原样存——能不能装库不该决定能不能记笔记。
新建笔记可套内置骨架 `template=lecture`（`## 定义 / ## 定理 · 结论 / ## 易错点 / ## 例题`）：`note_node_create` 与 `POST /notes/api/nodes` 都接受这个字段，显式给 `body_md` 时以正文为准；壳的设置里勾「新建笔记套用骨架」即默认带上。

三把钥匙的分工（工具作用域与 REST 作用域是两套，别混）：

| 钥匙 | 形式 | 能做什么 |
|---|---|---|
| 主设备 | `pho-rw:note_*…` + `notes_read\|notes_write` | 壳的全部读写 |
| 只读设备 | `pho-ro:notes_read`（REST）+ `note_tree\|note_get\|note_list_captures`（工具） | 看、导出、投屏 |
| **给 Agent** | `pho-agent:note_tree\|note_get\|note_list_captures` | 只读窄钥匙：能看树与笔记，写操作在统一入口就被 403 拦下 |

REST 层（`/notes/api/*`，挂载由 `DRIVER_NOTES_UI=1` 控制）给壳用：读操作要 `notes_read`、写操作要 `notes_write` 作用域；上传仍是 JSON base64（不引 `python-multipart`），单图上限 8 MB。AI 相关端点：`GET /notes/{id}/messages`、`POST /notes/{id}/summarize`、`POST /notes/{id}/chat`、`POST /notes/{id}/apply`；备份与同步：`POST /backup`、`GET /sync/status`、`POST /sync/push`、`POST /sync/pull`。

AI 按 `DRIVER_NOTES_LLM` 四选一：

| 取值 | 行为 | 需要的配置 |
| --- | --- | --- |
| `off`（默认） | 调 AI 端点直接 400 `ai_not_configured` | 无 |
| `openai` | 打 OpenAI 兼容 `/chat/completions`（可指向任何兼容网关） | `DRIVER_NOTES_LLM_API_KEY`，可选 `_BASE_URL`/`_MODEL`（默认 `gpt-4o-mini`） |
| `anthropic` | 打 `/v1/messages` | `DRIVER_NOTES_LLM_API_KEY`，可选 `_MODEL`（默认 `claude-sonnet-4-5`） |
| `manual` | 不联网：把笔记正文 + 批注 + 图片路径打成上下文包返回，由人粘给网页版模型 | 无 |

模型被要求只回 `{"summary": "...", "edits": [{"find": "...", "replace": "..."}]}`；`find` 必须在正文里**恰好出现一次**才会被套用，否则整条跳过并在返回的 `skipped` 里报出来。`DRIVER_NOTES_LLM_MAX_IMAGES`（默认 6，1–20）控制随请求附几张截图。

备份与同步（P3）：`note_backup` 把 `phonote.db` + 全部媒体打成一个 zip 放到 `backups/`；WebDAV 同步给多设备用，配置四个环境变量后生效：

| 变量 | 默认 | 语义 |
| --- | --- | --- |
| `DRIVER_NOTES_WEBDAV_URL` | 空（=不启用） | WebDAV 根地址，组件会挂在 `<url>/phonote/` 下 |
| `DRIVER_NOTES_WEBDAV_USER` / `_PASSWORD` | 空 | Basic 认证；两者都空就不带认证 |
| `DRIVER_NOTES_WEBDAV_KEEP` | `5` | 远端保留几份数据库快照（最少 1） |

```bash
DRIVER_PLUGINS=pho_note DRIVER_NOTES_DIR="$HOME/PhoNote" \
DRIVER_NOTES_WEBDAV_URL="https://dav.example.com/notes" \
DRIVER_NOTES_WEBDAV_USER=me DRIVER_NOTES_WEBDAV_PASSWORD=secret \
  .venv/Scripts/python -m uvicorn driver_platform.main:app --port 8000

KEY=... # 带 note_sync_* 作用域的钥匙
curl -X POST http://127.0.0.1:8000/v1/tools/execute -H "X-API-Key: $KEY" \
     -d '{"tool":"note_sync_push","arguments":{}}'
```

远端布局：`phonote/manifest.json`（提交点，**最后**才写）、`phonote/db/<UTC 时间戳>-<sha8>.db`（`VACUUM INTO` 快照，按 `KEEP` 轮换）、`phonote/media/ab/cd/<sha256>.webp`（内容寻址，只传远端缺的）。push 时先补目录、只传缺图、传快照，最后写 manifest；pull 时校验快照 sha256、先把本地库备份到 `backups/pre-pull-<时间戳>.db`、再原子替换并补拉缺图。

约定是**单主设备**：不做双向字段级合并，本地有未推送改动时 pull 直接拒绝（判据是内容指纹——`VACUUM` 出来的快照与活动库文件字节不同，所以不能比文件 hash）。

> 打折说明：① `openai`/`anthropic` 两种模式只用 `httpx.MockTransport` 测过，没有对真实模型服务跑过；② 没有重试、限流与流式输出，一次请求最多带 6 张图；③ WebDAV 只在本机用假的 WebDAV 服务（真实 HTTP）验证过 push → 换目录 pull，没有连过真实的坚果云/Nextcloud；Electron 壳也从未实机启动过、`npm install` 未在本机执行。

### 一条命令起全平台（阶段 7）

```bash
docker compose up -d --build        # 驱动平台 + Prometheus + Grafana
curl http://localhost:8000/health   # {"status":"ok"}
# 浏览器打开 http://localhost:3000（admin / admin）：数据源与「驱动平台 · 阶段 5 概览」面板已自动装配
docker compose down -v              # 停掉并删除数据卷
```

默认配置就是「起来能看出东西」的一套：SQLite 插件 + 演示库 + 指标 + JSON 日志 + 强制鉴权。演示钥匙是 `admin-key:plugin_admin` 与 `read-key:search_database|echo`（生产请用 secrets 注入并改掉）：

```bash
curl http://localhost:8000/v1/tools -H "X-API-Key: read-key" | head -40
curl -X POST http://localhost:8000/v1/tools/execute -H "X-API-Key: read-key" \
  -H "content-type: application/json" \
  -d '{"tool": "search_database", "arguments": {"table": "customers", "filters": {"city": "上海"}, "limit": 2}}'
# → {"tool":"search_database","contract_version":"1.0","result":{"table":"customers",...,"row_count":2}}
```

只要本服务时用 `docker compose up -d --no-deps driver-platform`（不拉 1.94 GB 的 Grafana）。镜像与配置的取舍：`Dockerfile` 按「依赖 → 源码 → 部署资产」分层，`deploy/docker/entrypoint.py` 在启动前按 `DRIVER_SEED_DEMO_DB=1` 种一份演示库（SQLite 插件是**只读**打开库的，文件必须先存在），然后 `exec` 成 uvicorn 让 `docker stop` 的 SIGTERM 直达应用；Prometheus / Grafana 两个第三方镜像**钉了摘要**，抓取用服务名 `driver-platform:8000`（本机开发用的 `host.docker.internal:8000` 那套仍在 `docs/observability/`）；面板 JSON 全仓库只有一份——`docs/observability/grafana-dashboard.json` 既供容器自动装配，也供本机手工导入。CI 多了一个 `deploy` 任务：`docker compose config` 校验 → 构建镜像 → 起服务 → 等健康检查 → 冒烟 `/health`、`/metrics`、`search_database`。

Grafana 容器另外关掉了自带插件的预装与自动更新（`GF_PLUGINS_PREINSTALL_DISABLED` / `GF_PLUGINS_PREINSTALL_AUTO_UPDATE`）：Grafana 13 默认在启动时把自带插件升级到最新版，先停掉旧进程再从 `storage.googleapis.com` 下载，离线或受限网络下下载失败会**连带注销**自带的那份——包括 prometheus 数据源，表现是面板「Datasource not found」而平台侧一切正常。要升级 Grafana 请改 compose 里的镜像摘要。踩坑全过程见[阶段 7 说明](docs/stages/stage-7.md)的验收证据。

### 多副本与滚动更新（阶段 8）

```bash
# 3 副本 + nginx 网关（宿主 8000 交给网关，平台端口从 compose 里摘掉）
docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --build --scale driver-platform=3
curl.exe -s http://127.0.0.1:8000/health            # {"status":"ok"}

# 一边打流量一边逐个换副本；探针只要有一次失败就返回 1
python deploy/rollout.py --replicas 3 --probe-seconds 60 --build
python deploy/traffic_probe.py --seconds 30 --workers 4 --expect 200
```

- **网关不做服务发现，只做解析**：`deploy/nginx/conf.d/default.conf` 用 `resolver 127.0.0.11`（Compose 内置 DNS）
  动态解析服务名 `driver-platform`，再 `proxy_pass http://$backend:8000`；副本 IP 变了不用重载 nginx。
  `proxy_next_upstream … non_idempotent` 让「某个副本正在退出」这一瞬由**重试**兜底（当前工具全是只读查询）。
- **滚动靠脚本**：Compose 非 swarm 模式没有原生 rolling（`up -d` 是先停后起），`deploy/rollout.py`
  把「`stop --time 25` → `rm` → `up -d --no-deps --no-recreate --scale driver-platform=N` → 等健康」
  做成可重复的序列，`deploy/traffic_probe.py`（纯标准库）在整个窗口里持续打流量并报成功/失败与状态码分布。
  本机实测：8.7 万次请求经网关、**0 失败**，替换期间新旧镜像并存，见[阶段 8 说明](docs/stages/stage-8.md)的验收证据。
- **优雅停机**：`Dockerfile` 的 CMD 带 `--timeout-graceful-shutdown 20`，应用侧由 `main.py` 的 lifespan
  在退出阶段调用 `PluginHost.close()`（逆序卸载插件、逐个尽力而为）。`docker stop` 会先停收新连接再跑收尾，
  日志里能看到「进程收尾：卸载插件」。
- **抓全部副本**：`deploy/prometheus/prometheus.yml` 用 `dns_sd_configs` 按服务名发现所有副本，
  一份配置同时服务单副本与多副本；`instance` 默认是「副本 IP:端口」，`sum by (instance)` 就能看谁在答。
- **旧版 Agent 兼容**：`tests/test_compat.py` 把「允许的演进」钉成测试——顶层未知字段被忽略、
  `arguments` 里的未知字段被忽略、成功响应恒为 `{"tool","contract_version","result"}`、
  错误信封只承诺 `code`/`message`、已发布工具的 `contract_version` 保持 `1.0`。
  策略（1.x 能加什么、什么必须升主版本）写在[阶段 8 说明](docs/stages/stage-8.md)的「契约兼容策略」。
- 只想本地单副本时原来的 `docker compose up -d --build` 一字不用改；两个 overlay 文件是叠加关系。

### K8s 编排（阶段 9 收口）

Compose 是「一台机器一条命令」，K8s 是「交给编排层」——副本数、自愈、滚动发布、资源上限都由集群负责：

```bash
kubectl apply -k deploy/k8s            # Namespace + Secret + Deployment + Service + HPA + PDB
kubectl -n driver-platform rollout status deploy/driver-platform
kubectl -n driver-platform get pods,svc,hpa,pdb
```

- 清单在 `deploy/k8s/`：`deployment.yaml`（3 副本、`maxUnavailable: 0`、只读根文件系统、非 root、drop ALL、
  `/health` 探针、128Mi/512m 资源上限）、`service.yaml`（ClusterIP 80 → 8000）、`hpa.yaml`（3→8 副本，CPU 70%）、
  `pdb.yaml`（驱逐时至少留 2 个）、`secret.example.yaml`（演示钥匙占位，真部署用 `kubectl create secret` 覆盖）。
- **数据卷是 emptyDir**：SQLite 不能多副本共享写，所以每个副本一份演示库（入口脚本启动时种）。
  真要多副本共享数据，应开 PostgreSQL 插件（`DRIVER_PLUGINS=postgres`），而不是把 `/data` 挂成 RWO 卷。
- **诚实标注**：本机没有可用集群（Docker Desktop 内置 Kubernetes 未能启动、`kind` 的二进制下载被网络阻断），
  所以这批清单只做到「结构静态校验」（`tests/test_k8s_manifests.py`，7 个用例），**没有在真集群上 apply 过**。
  校验命令 `kubectl apply --dry-run=client` 也需要 API server 做 discovery，离线不可用——这点写进了已知限制。

## 版本、质量门禁与发布

当前版本 **0.1.0**（`src/driver_platform/__init__.py` 与 `pyproject.toml` 同一处真话，`docker-compose.yml` 的镜像标签跟着走；有个测试专门守这三点一致）。

四道本地门禁，CI 里跑同一套：

```bash
ruff check .
ruff format --check src tests deploy examples
mypy                                   # 覆盖 src + deploy + examples（不含 tests：mock 噪音大）
pytest --cov=driver_platform --cov-report=term-missing:skip-covered --cov-fail-under=90
```

CI 还跑 `bandit -r src -q` 与 `pip-audit`，外加两个非单元测试任务：`deploy`（compose 起全平台冒烟）与多副本配置静态校验（overlay 合并、`nginx -t`、验收脚本语法）。

发布口径：`v0.1.0` 打 git tag 并建 GitHub Release；镜像由 CI 的 `publish` 任务推到 GHCR（`ghcr.io/qw2007/driver-platform:<版本>` 与 `:sha-<7位>`，仅 master，`packages: write`）。
本机 token 没有 `write:packages` 权限，实测 `docker push` 报 `permission_denied: The token provided does not match expected scopes`，所以推送只走 CI。版本号语义：接口契约（工具名、入参、错误码、响应形状）不兼容地变才升主版本——见[阶段 8 的契约兼容策略](docs/stages/stage-8.md)。

## 目录结构

```
src/driver_platform/
  main.py               create_app()：装配配置、注册表、插件宿主、错误处理、路由
  config.py             Settings.from_env()：插件 / SQLite 路径 / 协议 / 鉴权模式 / 钥匙清单 / 远程插件与超时
  bootstrap.py          create_registry_and_host()：内置工具 + 按配置装载插件
  errors.py             PlatformError 体系 + 统一错误信封
  security/             凭证与作用域：keys（解析与主体）、policy（authenticate + 可见性）、http（请求头 → 依赖）
  api/tools.py          GET /v1/tools、POST /v1/tools/execute
  api/plugins.py        GET /v1/plugins、POST /v1/plugins/remote、DELETE /v1/plugins/remote/{plugin_id}
  tools/                Tool 契约、ToolRegistry、executor（HTTP 与 MCP 共用执行路径）、echo 内置工具
  plugins/              Plugin 契约、PluginHost、插件目录、sqlite 插件、remote（远程插件与 HTTP 客户端）、
                        pho_note（PhoNote 笔记插件：树 / 截图缓冲区 / 组笔记 / 批注 / 版本 / AI / REST / 备份与 WebDAV 同步）
  protocols/            协议适配器：__init__（协议开关 + 路由装配）、openai_schema（纯翻译）、mcp（stdio 服务端）、mcp_server（进程入口）、routes
  observability/        request_id 上下文、四个指标、结构化日志（text/json）、ASGI 中间件、/metrics 路由
examples/               可运行的远端插件参考实现（remote_echo_service.py，供本机人工验收）
deploy/                  容器入口脚本、演示库种子（原子安装）、Prometheus 抓取配置、Grafana provisioning、
                         nginx 网关配置、多副本滚动脚本（rollout）与流量探针（traffic_probe）
tests/                  注册表/接口契约/插件宿主/配置/search_database/协议适配/安全权限/可观测性/远程插件/部署描述/
                        进程生命周期/契约兼容/PhoNote 存储、工具、AI 与 REST 测试
.github/workflows/ci.yml  CI：3.11/3.12/3.14 矩阵（ruff + format + mypy + pytest --cov-fail-under=90 + bandit + pip-audit 在主版本）、
                        部署冒烟（compose 校验/构建/健康检查）、GHCR 发布（仅 master，packages: write）
                        与多副本配置静态校验（overlay 合并、nginx -t、验收脚本语法）
docker-compose.yml       阶段 7：一条命令起全平台（driver-platform + Prometheus + Grafana，第三方镜像钉摘要）
deploy/k8s/              K8s 清单：Deployment / Service / HPA / PDB / Secret 占位 + kustomization（`kubectl apply -k`）
docker-compose.scale.yml 阶段 8 overlay：摘掉平台端口、加 nginx 网关（`--scale` 配合使用，不改动基础文件）
Dockerfile               运行镜像（分层安装 + HEALTHCHECK + 容器入口脚本）
.dockerignore            构建上下文收窄：.venv / .git 不出现在发往守护进程的上下文里
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
- [阶段 6：远程插件](docs/stages/stage-6.md) —— 远端契约 1.0（`/manifest` 与 `/execute`）、管理接口的三道门、错误码映射、同步/异步的边界
- [阶段 7：部署交付](docs/stages/stage-7.md) —— compose 编排、镜像分层与摘要钉版、Grafana provisioning、演示库种子、CI 的部署冒烟
- [阶段 8：生产加固](docs/stages/stage-8.md) —— 多副本与网关、滚动替换与探针、优雅停机的两级窗口、抓全副本、契约兼容策略（1.x）

项目级文档：

- [任务书：整体目标与 9 阶段验收标准](docs/task-brief.md)
- [交接说明：换机器/换会话继续开发前必读](docs/handoff.md)
- [变更日志：阶段 0–8 的逐阶段实现记录与验收证据](CHANGELOG.md)
- [环境与工具手册：本机工具链、提权清单、沙箱约束与 GitHub/Docker 直连解法](docs/environment.md)

## 已知限制

- CI 跑 3.11 / 3.12 / 3.14 三档矩阵，但本机只有 3.14——3.11/3.12 的结论完全来自 CI，本地无法对照复现。
- 工具执行无超时与并发限制，细节见阶段 1 说明的「已知限制」。
- SQLite 插件单连接串行、`total` 多一次 `COUNT(*)`，细节见阶段 2 说明的「已知限制」。
- MCP 侧只做 stdio 与 tools（无 HTTP/SSE、无 resources/prompts、一次一个客户端），协议版本跟随 SDK，`output_model` 即协议输出契约，细节见阶段 3 说明的「已知限制」。
- 鉴权默认关且只到「工具名」这一层：不配 `DRIVER_AUTH_MODE=required` 就是开放的，密钥明文放在环境变量里，没有速率限制、配额、审计与吊销，细节见阶段 4 说明的「已知限制」。
- 指标默认关、`/metrics` 无鉴权且是**进程内**的（重启归零，多副本各算各的）；`X-Request-ID` 只在**本进程内**贯穿，跨进程靠 W3C `traceparent`（只做 trace 传播，不做 span 树），日志不记请求体，直方图用默认桶（5 ms–10 s），细节见阶段 5 说明的「已知限制」。
- 远程插件默认关、清单只在装载时拉一次、无重试/熔断/签名校验，远端地址是单点，每次调用新建 HTTP 客户端（不复用连接池），细节见阶段 6 说明的「已知限制」。
- 默认那份 `docker-compose.yml` 是**单副本**：容器已改成非 root（uid 10001）+ 只读根文件系统 + `no-new-privileges` + 512m/1 CPU 限额，但 镜像已改成多阶段 + `python:3.14-alpine`（245 MB → 118 MB，代价是 musl 而非 glibc）、Prometheus / Grafana 钉的摘要是 amd64 平台摘要、基础镜像用标签（安全补丁自动进来、旧版也自动进来），`docker compose down -v` 会删数据卷，细节见阶段 7 说明的「已知限制」。
- 多副本是**单机 Compose 的 `--scale`**，不是编排：nginx 解析有 5 秒窗口（靠重试兜底）、重试对非幂等请求也开着、
  副本不会自动补回、多副本共享同一份只读数据卷、探针只衡量成功率不衡量延迟分布；`/metrics` 经网关暴露且仍无鉴权；
  远程插件的 HTTP 客户端会继承宿主机代理设置。细节见阶段 8 说明的「已知限制」。
- PostgreSQL 插件只做「只读等值查询」，没有事务控制、连接池、SSL 参数可调，`connect_timeout` 固定 5 秒；没装 `psycopg` 时插件装载直接失败（不降级）。
- PhoNote 的截图压缩依赖 Pillow：没装时图片按原样落盘（不报错，但体积不受控）；`/metrics` 默认仍公开（要收紧就开 `DRIVER_METRICS_PROTECTED=1`）；compose 里的演示钥匙是**默认值**，对外部署前必须用 `.env` 覆盖。
- 本机（Windows + DSH 沙箱）有三处环境级坑：pip / 全量 pytest / docker / **联网 Git 操作**需提权，GitHub 需 `sslBackend=schannel` 直连，Docker 需镜像加速——**这些只是本机噪声，不影响 CI**，逐条解法见[环境与工具手册](docs/environment.md)。
