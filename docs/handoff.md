# 交接说明：在新设备上继续开发「驱动平台」

> 目的：换一台机器、用一个全新的 DSH 会话，也能只靠**本文件 + GitHub 上的代码**把工作接着往下做。
> 仓库：`https://github.com/qw2007/DRiver-api`（**私有**，新设备需要可用的 GitHub 凭据）
> 交接时的提交：`7f34f26`（`master`，与 `origin/master` 同步）｜16 个提交 / 82 个受控文件

---

## 0. 一页速览

| 项 | 现状 |
|---|---|
| 项目 | 连接 AI Agent（Codex / Claude / ChatGPT…）与数据库等工具的**中间层驱动平台**：工具总线 + 协议适配 + 插件宿主（不是业务系统） |
| 技术栈 | Python 3.11+（本机 3.14，CI 跑 3.11 / 3.12 / 3.14 矩阵）、FastAPI、pydantic v2、uvicorn；MCP 走官方 SDK（可选依赖）；镜像 `python:3.14-alpine` 多阶段构建（118 MB） |
| 进度 | **阶段 0–8 已完成并推送**，另加 5 个非阶段交付（PhoNote 插件 P0–P3、两轮收口）；包版本 `0.1.0`，tag `v0.1.0` + GitHub Release（见第 3 节） |
| 门禁现状 | `pytest` **391 个用例、0 失败**（3 个 PostgreSQL 集成用例本机跳过，CI 带 `postgres` service 会真跑）、覆盖率 **93.12%**（2719 语句 / 187 未覆盖，CI 卡 90% 底线）；`ruff check .` / `ruff format --check src tests deploy examples`（79 files）/ `mypy`（47 files，含 `deploy/` 与 `examples/`）/ `bandit` 全绿 |
| CI | 三个任务：`test`（Python **3.11 / 3.12 / 3.14** 矩阵 + `postgres` service）、`deploy`（compose 建镜像 + 健康轮询 + 接口冒烟）、`publish`（master 上推 GHCR `ghcr.io/qw2007/driver-platform:<版本>` 与 `:sha-<7位>`）；`deploy` 里还含多副本 overlay / `nginx -t` / 验收脚本语法的静态校验 |
| 一键起全平台 | 仓库根执行 `docker compose up -d --build` → 平台 `:8000`、Prometheus `:9090`、Grafana `:3000`（`admin`/`admin`）；多副本见 `docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --scale driver-platform=3` |
| 下一步 | 九阶段目标与四轮收口都已完成（四项决策 + 八项补齐 + 五项编排分发）。**唯一没验证的是 K8s 清单**：本机起不来集群、`kind` 也下载不到，清单只做了结构静态校验（`tests/test_k8s_manifests.py`）；其余待办都要外部资源（GHCR 私有镜像的 `imagePullSecrets`、HPA 的 metrics-server） |

---

## 1. 项目要达成什么

**定位**：AI Agent 与工具之间的中间层，提供「工具总线 + 协议适配 + 插件宿主」。原型式演进，每阶段独立可验收。

**九阶段验收标准**（原文见 [`task-brief.md`](task-brief.md) 第三节）：

| 阶段 | 目标 | 验收标准 | 状态 |
|---|---|---|---|
| 0 | 骨架 + CI | `GET /health` 返回 ok；CI 跑 pytest + ruff | ✅ |
| 1 | 核心服务 + 工具注册 | `execute` 统一入口；echo 工具；非法参数 → 422 | ✅ |
| 2 | 插件系统 + 数据库插件 | 工具列表出现 `search_database`；插件可注册/注销；结构化结果 | ✅ |
| 3 | 协议适配层 | MCP 客户端能 list/call `search_database`；curl 返回 OpenAI 工具 JSON Schema | ✅ |
| 4 | 安全权限 | 无凭证被拒绝；越权被拦 | ✅ |
| 5 | 可观测性 | Grafana 看 QPS/延迟/错误率；日志按 `request_id` 追全链路 | ✅ |
| 6 | 远程插件 | 远程注册可调用；插件下线后工具自动不可用 | ✅ |
| 7 | 部署交付 | `docker compose up` 起全平台；CI/CD 通过 | ✅ |
| 8 | 生产加固 | 多副本；滚动更新不中断；旧版 Agent 兼容 | ✅ |

**八条原则**（约束，不是建议）：接口先行 / 插件契约版本化（破坏性变更升主版本）/ 适配器隔离（MCP、OpenAI、LangChain 互不依赖）/ 配置外部化（只从环境变量读）/ 测试从阶段 0 开始 / 可观测性内建 / 文档随代码同步 / 特性开关默认关。

**每阶段的交付规范**：先出设计说明（`docs/stages/stage-N.md`，含五件套：技术选型理由 / 模块设计 / 接口定义 / 关键代码示例 / 测试与部署方案），再落地代码；结束时四道门禁全绿。

---

## 2. 新设备上十分钟拉起

### 2.1 先决条件

- Python **3.11+**（本项目在 3.14 上开发与 CI；`requires-python = ">=3.11"`，3.11 / 3.12 未实测）
- Git，并且有权限访问该**私有**仓库（HTTPS 凭据或 SSH key）
- Docker Desktop（阶段 7/8 需要；阶段 0–6 不需要）

### 2.2 从零到能跑测试

```bash
git clone https://github.com/qw2007/DRiver-api.git driver-platform-v2
cd driver-platform-v2

python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"     # Windows: .venv\Scripts\python.exe

# 四道门禁（与 CI 完全一致）
.venv/bin/ruff check .
.venv/bin/ruff format --check src tests deploy examples
.venv/bin/mypy
.venv/bin/pytest --cov=driver_platform --cov-report=term-missing:skip-covered --cov-fail-under=90
.venv/bin/bandit -r src -q
.venv/bin/python -m pip_audit --progress-spinner off
```

后两条是 CI 的安全扫描；PostgreSQL 插件的集成测试要真库，没设 `DRIVER_TEST_POSTGRES_DSN` 时会自动 skip（例如 `docker run -d --name dp-pg -e POSTGRES_PASSWORD=secret -e POSTGRES_USER=demo -e POSTGRES_DB=shop -p 55432:5432 postgres:16-alpine`，然后 `DRIVER_TEST_POSTGRES_DSN=postgresql://demo:secret@127.0.0.1:55432/shop`）。

**Windows 受限沙箱的额外一步**（本机实测，见 [`environment.md`](environment.md)）：受限模式下 `python -m venv .venv` 会在 ensurepip 阶段失败（临时目录 DACL 问题）。做法是
`python -m venv --without-pip .venv`，再用**提权**执行 `.venv\Scripts\python.exe -m ensurepip --upgrade` 与 `.venv\Scripts\python.exe -m pip install -e ".[dev]"`。

### 2.3 起服务（两种方式）

```bash
# 方式 A：本机直接跑（开发调试）
.venv/bin/python -m uvicorn driver_platform.main:app --host 0.0.0.0 --port 8000

# 方式 B：全平台（平台 + Prometheus + Grafana）
docker compose up -d --build
docker compose ps          # 三个服务都应 Up，平台 healthy
```

冒烟（方式 B 里的演示钥匙，来自 `docker-compose.yml` 的默认值）：

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/v1/tools -H "X-API-Key: read-key"
curl -s -X POST http://127.0.0.1:8000/v1/tools/execute \
  -H "Content-Type: application/json" -H "X-API-Key: read-key" \
  -d '{"tool":"search_database","arguments":{"table":"customers","filters":{"city":"上海"},"limit":2}}'
```

`docker compose down` 收尾（`-v` 会连三个数据卷一起删）。

### 2.4 可选：MCP stdio 入口、远程插件示例

```bash
# MCP stdio 入口（需要 mcp 额外依赖，dev 里已包含）
.venv/bin/python -m driver_platform.protocols.mcp_server

# 阶段 6 的远端参考实现（另一个进程，默认 127.0.0.1:9100）
.venv/bin/python examples/remote_echo_service.py
```

---

## 3. 接下来做什么

### 3.1 阶段 8「生产加固」（已完成，这里是当时的验收点与结论）

| 验收点 | 阶段 8 的做法 | 结论与证据 |
|---|---|---|
| 多副本 | 保持「无进程内状态」：`docker-compose.scale.yml` overlay 摘掉平台端口、前置 nginx 网关；`--scale driver-platform=3` | 三个副本经网关分流，`count(up{job="driver-platform"} == 1) == 3`；Prometheus 改 `dns_sd_configs` 按服务名发现，`instance` 区分副本 |
| 滚动更新不中断 | `deploy/rollout.py`（停一个 → 补一个 → 等健康）+ `deploy/traffic_probe.py`（纯标准库持续打流量）；优雅停机是 uvicorn `--timeout-graceful-shutdown 20` + lifespan 里的 `PluginHost.close()` | 经网关 86875 次请求 **0 失败**，替换期间新旧镜像并存，三个副本始终 healthy；`docker stop` 日志可见「进程收尾：卸载插件」 |
| 旧版 Agent 兼容 | `tests/test_compat.py` 把隐式承诺钉成断言（未知字段被忽略、响应三键固定、错误信封只承诺 `code`/`message`、已发布工具 `contract_version` 保持 `1.0`），策略写在阶段说明的「契约兼容策略（1.x）」 | 兼容策略有文档、有测试；破坏性变更的判据（什么必须升主版本）写清楚了 |

细节与逐条证据见 [`stage-8.md`](stages/stage-8.md)（选型 / 模块设计 / 接口定义 / 关键代码 / 测试与部署 / 非目标 / 已知限制 / 验收证据）。

### 3.2 已知未做 / 明确留白（都不是 bug，是范围选择）

1. ~~CI 覆盖率门禁未加~~ **已补**：CI 现在跑 `pytest --cov=driver_platform --cov-fail-under=90`（本地 94%，门禁卡 90% 是防一路下滑的底线）。
2. ~~`bandit` / `pip-audit` 未进 CI~~ **已补**：CI 增加了 `bandit -r src -q` 与 `python -m pip_audit --progress-spinner off` 两步。
3. ~~CI 只跑 Python 3.14~~ **已补**：矩阵 `3.11 / 3.12 / 3.14`；静态检查、覆盖率门禁、bandit 与 pip-audit 只在 3.14 上跑，低版本只跑 `pytest -q`（结论以 CI 为准，本机无法对照复现）。
4. ~~PostgreSQL 插件未落地~~ **已补**：`search_postgres`（`src/driver_platform/plugins/postgres.py`）复用 SQLite 的入参/输出模型，只读会话 + `connect_timeout=5`，可选依赖 `pip install 'driver-platform[postgres]'`，配置 `DRIVER_POSTGRES_DSN`；真实集成在本机 `postgres:16-alpine` 上跑过。
5. **`mypy` 已覆盖 `src` + `deploy` + `examples`**（47 个文件），仍**不含 `tests/`**（mock 噪音大于收益）。
6. **包版本已定 `0.1.0`**（`pyproject.toml` 与 `src/driver_platform/__init__.py` 同源，compose 镜像标签跟随），发布物是 git tag `v0.1.0` + GitHub Release + **CI 的 `publish` 任务推 GHCR**（`:0.1.0` 与 `:sha-<7位>`；本机 token 无 `write:packages`，所以本机不推）。
7. **阶段 6 远程插件的留白**：清单缓存 60 秒、连接失败重试一次（读超时与 5xx **不**重试）；仍无熔断/热更新/签名校验；`base_url` 是单点；每次调用新建 HTTP 客户端；客户端会继承宿主机代理设置。
8. **阶段 5 可观测性留白**：指标默认关、`/metrics` 默认无鉴权（可开 `DRIVER_METRICS_PROTECTED=1` 走同一套凭证）、指标是进程内的（重启归零）；跨进程链路已接 W3C `traceparent`（进站解析、出网传播、日志带 `trace_id`），但只做传播、不采样也不导出 span。
9. **阶段 7 / 8 / 9 部署留白**：默认仍是单副本；容器已非 root（uid 10001）+ 只读根文件系统 + `no-new-privileges` + 512m/1 CPU，镜像换 `python:3.14-alpine` 多阶段后 **118 MB**（此前 245 MB），代价是 musl；第三方镜像仍钉 amd64 摘要、基础镜像用标签、compose 演示钥匙是明文默认值（对外必须 `.env` 覆盖）；多副本在 compose 里是单机 `--scale`，K8s 清单（`deploy/k8s/`，3 副本 + `maxUnavailable: 0` + HPA + PDB）**未在真集群 apply 过**——本机起不来集群、`kind` 也下载不到。

### 3.3 当年的四个开放项（已全部拍板）

| 问题 | 结论 |
|---|---|
| CI 加 `--cov-fail-under`？ | **加**，阈值 90%（`.github/workflows/ci.yml` 的 test 步骤） |
| `bandit` / `pip-audit` 进 CI？ | **进**，与覆盖率门禁同一 job |
| 是否上真正的编排层（K8s）？ | **上，但只到清单层**：`deploy/k8s/` 给了 Deployment/Service/HPA/PDB/kustomization 与结构测试，`kubectl apply -k` 一行起全套；本机没有可用集群，**未做真集群验收** |
| 技术栈换不换？ | **不换**，继续 Python + FastAPI（九个阶段与 PhoNote 全建在上面） |

产出这一组结论的对话在 2026-11-13（用户逐条确认）。若将来要推翻其中任何一条，先改 `docs/stages/stage-7.md` / `stage-8.md` 的对应决策记录，再动代码。

---

## 4. 在 DSH 里怎么接着开发

### 4.1 新会话的开场白（可直接复制）

> 项目在 `D:\DSh\driver-platform-v2`（或新设备上的路径），GitHub 是 `qw2007/DRiver-api`，已同步到 `master`。
> 先读 `docs/handoff.md`、`docs/task-brief.md`、`CHANGELOG.md` 与 `docs/stages/stage-8.md` 了解现状（九阶段已全部完成），
> 然后按第 3.2 / 3.3 节挑下一步：先拍板待定项，或直接开工最接近生产的那几条（镜像瘦身与非 root 运行、`/metrics` 收进内网、PostgreSQL 插件）。
> 每阶段的固定动作不变：先出设计说明再落地，结束时 `ruff check .` / `ruff format --check src tests deploy examples` / `mypy` / `pytest` 全绿并保持 CI 可跑。

如果要开长任务，可以用 `/goal` 并把上面这段作为目标；本仓库之前的 goal 目标原文是：

> 按 9 阶段推进「驱动平台」（AI Agent ↔ 数据库等工具的中间层）：阶段 0 骨架+CI 已完成并提交，后续按阶段 1→8 逐阶段推进，每阶段先出设计说明再落地代码，每阶段结束时 ruff check / ruff format --check / pytest 全绿并保持 CI 可跑。

### 4.2 每阶段的固定动作

1. 读 `CHANGELOG.md` 的「当前状态」+ 上一阶段说明 → 在 `docs/stages/stage-N.md` 写设计说明（五件套 + 环境变量表 + 非目标 + 已知限制 + 验收证据占位）；
2. 落地代码 + 测试（测试与代码同批提交）；
3. 跑门禁：`ruff check .` → `ruff format --check src tests deploy examples` → `mypy` → `pytest`（要覆盖率再加 `--cov=driver_platform --cov-report=term-missing:skip-covered`）；
4. 本机真跑验收（阶段 7 起用 `docker compose up -d --build`），把**真实响应**粘进该阶段说明的「验收证据」；
5. 回填 `CHANGELOG.md`（概览表一行 + 完整阶段小节 + 「当前状态」+ 已知限制）与 `README.md`（路线表、目录结构、用法、阶段文档链接）；
6. 提交两个（一个 `feat(stage-N)`、一个 `docs`）并推送；
7. 看 CI：`gh run list`（需先 `gh auth login`）或网页 Actions 页。

### 4.3 提交与推送约定

- 提交信息用中文，格式参照历史：`feat(stage-7): 部署交付——compose 起全平台、Grafana 自动装配与 CI 部署冒烟`、`docs: 变更日志与任务书补阶段 7`；
- 只推 `master` 一个分支，不保留其他分支；
- **推送前**：`git fetch` 看一眼远端有没有新提交（仓库主人会直接在 GitHub 网页上改 `docs/task-brief.md` 的阶段状态，之前就撞过一次 → `git rebase origin/master` 解决）；
- Windows 上联网 Git 命令需要 HTTPS 走 schannel 且关掉吊销检查：
  `git -c http.sslBackend=schannel -c http.schannelCheckRevoke=false push origin master`。

---

## 5. 新设备的环境准备与已知坑

以下都是本机实测踩出来的，换机器时按需重做（细节与命令见 [`environment.md`](environment.md)）：

1. **提权清单（Windows 受限沙箱）**：`pip install`、全量 `pytest`（带 `tmp_path` 的用例）、`docker` 命令、联网 `git` 都必须提权。
   根因：pytest 用 `Path.mkdir(mode=0o700)` 建临时目录，受保护 DACL 下 `BUILTIN\Administrators` 对受限令牌是 deny-only，**创建者自己都进不去**；Docker CLI 要开命名管道。
   若新机器工作区也报 `grantWrite(...)` 失败，用技能 `diagnose-windows-sandbox-acl` 修的典型结论是「缺 `WRITE_OWNER`」。
2. **PowerShell 5.1（不是 pwsh 7）**：`Get-Content <f> -Raw` 默认按 GBK 解码 UTF-8 → 中文乱码；读文件加 `-Encoding UTF8`，写文件用 `[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))`；**不支持 heredoc `<<`**（提交信息写进 `.tmp/commit-msg.txt` 再 `git commit -F`）。
3. **GitHub 直连**：若 `git` 拉不动，检查 `http.sslBackend=schannel`、`http.schannelCheckRevoke=false`，并删掉失效的 ghproxy `insteadOf` 规则。
4. **Docker 拉镜像**：`~/.docker/daemon.json` 加 `registry-mirrors`（本机用的是 daocloud / 1panel / xuanyuan / 1ms），然后重启 Docker Desktop。`docker buildx imagetools inspect` 会绕过镜像加速直连 `registry-1.docker.io`，取第三方镜像摘要时会失败 → 摘要以 `docker images --digests` 为准。
5. **不要设 `$env:PYTHONIOENCODING`**：会让 spawn 子进程的用例在 gbk 解码上假失败。
6. **`gh` CLI 未登录**（本机）：`gh run list` 会报 `please run: gh auth login`；临时替代是用本机已保存的 git 凭据读 REST API：
   `curl -s -H "Authorization: Bearer <token>" https://api.github.com/repos/qw2007/DRiver-api/actions/runs?head_sha=<sha>`。

---

## 6. 文件地图

| 路径 | 是什么 |
|---|---|
| [`README.md`](../README.md) | 项目入口：路线表、快速开始、每个阶段怎么用、已知限制 |
| [`CHANGELOG.md`](../CHANGELOG.md) | 每个阶段的目标 / 交付物 / 关键决策与理由 / 验收证据 + 「当前状态」+「已知限制（汇总）」 |
| [`task-brief.md`](task-brief.md) | 原始任务书：九阶段验收标准、八条原则、技术栈、交付规范 |
| [`environment.md`](environment.md) | 本机工具清单、提权清单、GitHub 与 Docker 的网络解法、一分钟自检 |
| `stages/stage-0..8.md` | 各阶段设计说明（固定章节：选型理由 / 模块设计 / 接口定义 / 关键代码 / 测试与部署方案 / 非目标 / 已知限制 / 验收证据） |
| [`../docker-compose.yml`](../docker-compose.yml) | 一条命令起全平台（平台 + Prometheus + Grafana，含演示钥匙与健康检查） |
| [`../docker-compose.scale.yml`](../docker-compose.scale.yml) | 阶段 8 overlay：摘掉平台端口 + nginx 网关（配合 `--scale driver-platform=N` 使用） |
| [`../Dockerfile`](../Dockerfile) | 分层构建 + `HEALTHCHECK` + 入口脚本 `exec` + 优雅停机窗口与构建戳 |
| `../deploy/` | 容器入口脚本、演示库种子（原子安装）、Prometheus 抓取配置、Grafana provisioning、nginx 网关配置、`rollout.py`（滚动替换）与 `traffic_probe.py`（流量探针） |
| [`observability/`](observability) | 本机联调用的 Prometheus 配置与 Grafana 面板 JSON（同一份面板也挂在 compose 里） |
| `../src/driver_platform/` | 源码：`main.py`（应用装配 + lifespan）、`config.py`（环境变量与校验）、`tools/`（契约/注册表/执行器）、`plugins/`（宿主/契约/catalog/SQLite/remote）、`protocols/`（MCP 与 OpenAI Schema）、`security/`、`observability/`、`api/` |
| `../tests/` | 23 个测试文件（另加 `conftest.py`）/ 276 个用例 |
| [`../examples/remote_echo_service.py`](../examples/remote_echo_service.py) | 阶段 6 的远端参考实现，用于人工验收远程插件 |

环境变量全集（代码里唯一真话在 `src/driver_platform/config.py` 与 `security/keys.py`）：
`DRIVER_PLUGINS`、`DRIVER_SQLITE_PATH`、`DRIVER_PROTOCOLS`、`DRIVER_METRICS_ENABLED`、`DRIVER_LOG_LEVEL`、`DRIVER_LOG_FORMAT`、`DRIVER_REMOTE_PLUGINS`、`DRIVER_REMOTE_PLUGIN_ENABLED`、`DRIVER_REMOTE_TIMEOUT`、`DRIVER_API_KEYS` / `DRIVER_API_KEY`、`DRIVER_AUTH_MODE`；容器专用的 `DRIVER_SEED_DEMO_DB`（入口脚本读）。

---

## 7. 一分钟自检（判断"接上了"没有）

```bash
git log --oneline -1                     # 应看到最新提交（阶段 8 之后）
.venv/bin/pytest -q                      # 276 passed（或更多）
.venv/bin/ruff check .                   # All checks passed
docker compose up -d --build && docker compose ps
curl -s http://127.0.0.1:8000/health     # {"status":"ok"}

# 想验多副本与滚动（阶段 8）：
docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --scale driver-platform=3
python deploy/traffic_probe.py --seconds 20 --workers 4   # failed 应为 0
```

四项都正常，就说明代码、环境、门禁、部署四条线都接上了；九阶段目标已收口，接下来按第 3.2 / 3.3 节挑活。
