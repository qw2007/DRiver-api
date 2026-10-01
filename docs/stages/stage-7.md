# 阶段 7：部署交付（compose 起全平台 + CI 部署冒烟）

> 验收目标：`docker compose up` 一条命令起全平台；CI/CD 通过。

## 选型理由

- **Docker Compose，而不是 K8s / Helm**：本阶段要证明的是「一台机器、一条命令、三个服务、
  可复现」，Compose 是这个目标的最小工具——部署描述就是仓库里的一个文件，没有集群、
  没有 CRD、没有控制面要维护。多副本、滚动更新、就绪探针是阶段 8 的事，那时再谈编排层的
  能力才有对照物（现在的 compose 文件就是对照物）。
- **第三方镜像钉摘要，不用 `latest`**：`prom/prometheus@sha256:…`、`grafana/grafana@sha256:…`
  （摘要记录在[环境与工具手册](../environment.md) §2）。`latest` 会让「同一份 compose 起同一个
  镜像」不成立。本机在阶段 5 手工起容器时正撞上这一点：当时拉到的是 Grafana 13.2.3，
  首次启动迁移 813 次仍未开始监听，同一个 `latest` 明天可能是另一份东西。
- **本服务用 `build: .` 而不是推镜像仓库**：`docker compose build` 本身就是构建证明，CI 里跑得通；
  推仓库需要 registry 凭据、镜像 tag 与版本号策略（包版本现在还是 `0.0.0`），属于交付流水线的
  范围，本阶段不引入（见非目标）。
- **基础镜像用 `python:3.14-slim` 标签，不钉摘要**：与上一条相反的选择，理由是两者要的东西不同——
  第三方服务要的是**行为可复现**（它们自己不随我们的代码变），基础镜像要的是**安全修复能自动进来**。
  取舍记入已知限制。
- **Grafana 用 provisioning 文件，而不是「打开浏览器点一遍」**：数据源、面板都以仓库里的文件为准，
  `docker compose up` 之后打开 3000 端口就能看；也让面板**只有一份真话**——
  `docs/observability/grafana-dashboard.json` 既供容器自动装配，也供本机手工导入
  （为此去掉了阶段 5 的 `${DS_PROMETHEUS}` 占位符，改成固定 uid `prom-main`，因为 provisioning
  不做占位符替换，面板会静默变成「Datasource not found」）。
- **容器入口脚本种一份演示库**：SQLite 插件是**只读**打开库的（阶段 2 的设计），没有种子文件就是
  「容器起来了，`search_database` 却查不了」——演示价值归零。种子只在 `DRIVER_SEED_DEMO_DB=1`
  且目标文件不存在时执行，幂等；生产设 `0` 并挂自己的库。
- **入口脚本用 Python 且 `os.execvp`**：Windows 上写 shell 脚本很容易带上 CRLF，容器里 `/bin/sh`
  会报 `not found`；用 Python 就没有换行符问题。`exec` 而不是 `subprocess`：让 uvicorn 成为 PID 1，
  `docker stop` 的 SIGTERM 直达应用，而不是被入口进程吃掉等超时——阶段 8 的「滚动更新不中断」
  要靠这条。
- **容器里不装 curl**：健康检查用 `python -c "urllib.request.urlopen(...)"`，镜像里本来就有 Python，
  为一个探针再装一个包管理器/二进制层不值得。

## 模块设计

```
docker compose up -d
      │
      ├── driver-platform  (build: .)   :8000  ── /health /metrics /v1/*
      │        entrypoint.py ──(DRIVER_SEED_DEMO_DB=1 且库不存在时种演示库)──> exec uvicorn
      │
      ├── prometheus       (钉摘要)     :9090  ── 抓 driver-platform:8000/metrics
      │
      └── grafana          (钉摘要)     :3000  ── provisioning: 数据源 prom-main + 阶段 5 面板
```

- `docker-compose.yml`：三个服务 + 三个命名卷（`data` / `prometheus-data` / `grafana-data`）。
  `driver-platform` 带健康检查，`prometheus` 用 `condition: service_healthy` 等它健康再启动——
  否则启动初期抓不到目标，看板上会先出现一段 `up=0`。
- `Dockerfile`：按「依赖 → 源码 → 部署资产」分层（改一行源码不重装依赖），`HEALTHCHECK` 让
  `docker run` 直接起时也能看出可用性，`ENTRYPOINT`（入口脚本）+ `CMD`（uvicorn）分离，
  想换成别的进程只覆盖 CMD。
- `.dockerignore`：`.venv/`、`.git/`、`tests/`、`docs/` 等不进构建上下文——不这样做，
  构建时会把数百 MB 的虚拟环境和全部历史发给守护进程。
- `deploy/docker/entrypoint.py`：可选种子 + `exec`。
- `deploy/demo/seed_demo_db.py`：`customers` / `orders` 两张表，与 README 的验收示例一一对应
  （`table=customers`、`filters={"city": "上海"}`），幂等（文件在就跳过）。
- `deploy/prometheus/prometheus.yml`：抓 `driver-platform:8000`；与阶段 5 的本机开发配置
  （`docs/observability/prometheus.yml`，抓 `host.docker.internal:8000`）只差抓取目标。
- `deploy/grafana/provisioning/`：`datasources/prometheus.yml`（uid `prom-main`，指向
  `http://prometheus:9090`，设为默认、不可编辑）与 `dashboards/driver-platform.yml`
  （面板目录 `/var/lib/grafana/dashboards`，`allowUiUpdates=false`、`disableDeletion=true`——
  容器里的改动重启即失效，想长期改就改仓库）。
- `.github/workflows/ci.yml`：新增 `deploy` 任务（校验 → 构建 → 起服务 → 等健康 → 冒烟 → 收尾）。
- `tests/test_deploy_config.py`：14 个**不起容器**的契约测试，见「测试与部署方案」。

## 接口定义

### 环境变量

compose 里的默认值（每一项都可以在宿主上用同名变量覆盖，`${VAR:-默认}` 语法）：

| 变量 | 默认 | 取值 | 语义 |
|---|---|---|---|
| `DRIVER_PORT` | `8000` | 端口 | 驱动平台在宿主上的端口 |
| `PROMETHEUS_PORT` | `9090` | 端口 | Prometheus UI / API 在宿主上的端口 |
| `GRAFANA_PORT` | `3000` | 端口 | Grafana UI 在宿主上的端口 |
| `GRAFANA_PASSWORD` | `admin` | 字符串 | Grafana 管理员密码（用户名固定 `admin`） |
| `DRIVER_API_KEYS` | `admin-key:plugin_admin,read-key:search_database\|echo` | 钥匙清单 | 演示钥匙，生产请用 secrets 注入 |
| `DRIVER_SEED_DEMO_DB` | `1` | `1/true/yes/on` 为真 | **入口脚本自己的开关**（平台不读它）：首次启动种演示库 |

compose 为平台预置的配置（对应阶段 2–6 的开关）：

| 变量 | compose 里的值 | 为什么是这个值 |
|---|---|---|
| `DRIVER_PLUGINS` | `sqlite` | 「起全平台」要包含数据库插件，否则只有 echo |
| `DRIVER_SQLITE_PATH` | `/data/demo.db` | 落在命名卷 `data` 上，容器重建不丢 |
| `DRIVER_METRICS_ENABLED` | `1` | 不打开就没有 `/metrics`，Prometheus 无从抓取 |
| `DRIVER_LOG_FORMAT` | `json` | `docker logs` 直接是结构化日志，与阶段 5 的契约一致 |
| `DRIVER_AUTH_MODE` | `required` | 演示「配了钥匙就必须有效」，也让越权路径可见 |

> 远程插件（阶段 6）在 compose 里**没有**预置：它需要远端地址，属于使用者的部署决策，
> 用法见[阶段 6 说明](stage-6.md)。

Grafana 自己的两个插件开关（**不是**平台变量，改了会影响看板能不能取数）：

| 变量 | compose 里的值 | 为什么 |
|---|---|---|
| `GF_PLUGINS_PREINSTALL_DISABLED` | `true` | 不预装、不更新自带插件 |
| `GF_PLUGINS_PREINSTALL_AUTO_UPDATE` | `false` | Grafana 13 默认启动时把自带插件升级到最新版，下载失败会**连带注销**自带的那份（含 prometheus 数据源），面板随即「Datasource not found」——见验收证据第 6 条 |

### 端口、卷与依赖

| 服务 | 宿主端口 | 命名卷 | 健康检查 | 依赖 |
|---|---|---|---|---|
| `driver-platform` | `${DRIVER_PORT:-8000}` → 8000 | `data:/data` | `GET /health`（5 s 一次，12 次重试） | — |
| `prometheus` | `${PROMETHEUS_PORT:-9090}` → 9090 | `prometheus-data:/prometheus` | 无（启动很快） | `driver-platform: service_healthy` |
| `grafana` | `${GRAFANA_PORT:-3000}` → 3000 | `grafana-data:/var/lib/grafana` | 无（首次启动迁移可能数分钟） | 顺序上在 `prometheus` 之后 |

### 对外入口

| 入口 | 地址 | 说明 |
|---|---|---|
| 平台健康 | `GET http://localhost:8000/health` | 永远公开，容器健康检查用的就是它 |
| 平台指标 | `GET http://localhost:8000/metrics` | 阶段 5 的四个指标，**无鉴权**（已知限制） |
| 工具清单 / 调用 | `/v1/tools`、`/v1/tools/execute` | 需要 `X-API-Key` 或 `Authorization: Bearer` |
| Prometheus | `http://localhost:9090` | 目标自检：`/api/v1/targets` |
| Grafana | `http://localhost:3000` | `admin` / `$GRAFANA_PASSWORD`（默认 `admin`），面板已装配 |

## 关键代码

`docker-compose.yml`：平台服务与健康检查（其余两个服务只是镜像 + 挂载）。

```yaml
  driver-platform:
    build:
      context: .
    image: driver-platform:0.0.0
    environment:
      DRIVER_PLUGINS: sqlite
      DRIVER_SQLITE_PATH: /data/demo.db
      DRIVER_SEED_DEMO_DB: "1"
      DRIVER_METRICS_ENABLED: "1"
      DRIVER_LOG_FORMAT: json
      DRIVER_AUTH_MODE: required
      DRIVER_API_KEYS: "${DRIVER_API_KEYS:-admin-key:plugin_admin,read-key:search_database|echo}"
    volumes:
      - data:/data
    ports:
      - "${DRIVER_PORT:-8000}:8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]
      interval: 5s
      timeout: 3s
      retries: 12
      start_period: 5s
    restart: unless-stopped
```

`deploy/docker/entrypoint.py`：种库（可选）然后把进程交出去——`exec` 是这里唯一的重点。

```python
    db_path = os.environ.get("DRIVER_SQLITE_PATH") or DEFAULT_DB_PATH
    if _seed_enabled() and not os.path.exists(db_path):
        subprocess.run([sys.executable, SEED_SCRIPT, "--path", db_path], check=True)
    os.execvp(argv[1], argv[1:])
```

`deploy/prometheus/prometheus.yml`：抓取目标从宿主名换成服务名，其余与阶段 5 完全一致。

```yaml
scrape_configs:
  - job_name: driver-platform
    metrics_path: /metrics
    static_configs:
      - targets: ["driver-platform:8000"]
        labels:
          instance: compose
```

`.github/workflows/ci.yml`：`deploy` 任务的核心——「compose 能起」不能只在开发机上验证过一次就算数。

```yaml
      - name: 等健康检查通过
        run: |
          container="$(docker compose ps -q driver-platform)"
          for _ in $(seq 1 30); do
            status="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
            if [ "$status" = "healthy" ]; then exit 0; fi
            sleep 2
          done
          docker compose logs driver-platform
          exit 1
```

## 测试与部署方案

**测试**（`tests/test_deploy_config.py`，14 个用例，不起容器——起容器是 CI 与本机验收的事）：
守的是「静态错、但不会在开发时暴露」的三类问题：

1. **配置名对不上**：compose 里每个 `DRIVER_*` 都必须是平台（`config.py` / `security/keys.py` 的
   `ENV_*` 常量）真的会读的名字，唯一的例外是入口脚本自己的 `DRIVER_SEED_DEMO_DB` 白名单。
   打错一个字母，容器起来就是启动失败或静默走默认值。
2. **可复现性**：两个第三方镜像必须形如 `repo@sha256:<64 位>`，本服务必须是 `build` + 固定本地 tag；
   `Dockerfile` 不许出现 `:latest`。另外校验 `.dockerignore` 确实排除了 `.venv/`、`.git/`、`tests/`、
   `docs/`。
3. **三处必须对得上**：Grafana 数据源 `uid` ↔ 面板 JSON 里每个面板的 `datasource.uid` ↔
   provisioning 的挂载路径与面板目录路径。对不上时 Grafana 不报错，只是面板显示
   「Datasource not found」。
4. 另外还钉住：演示库种子建出的 `customers` 表与 README 的验收示例是同一份 schema（示例能跑通而不
   只是看起来对）、种子幂等、入口脚本只在开关打开时种库且最后一定 `exec`。

**部署**：

```bash
docker compose up -d --build      # 起全平台
docker compose ps                 # 三个服务的健康状态
docker compose down -v            # 停掉并删数据卷
```

只起本服务（不拉 1.94 GB 的 Grafana）：`docker compose up -d --no-deps driver-platform`。

**CI**：`test` 任务（ruff check / ruff format --check src tests deploy examples / mypy / pytest）
之外新增 `deploy` 任务——`docker compose config --quiet` 静态校验 → `docker compose build
driver-platform` → `up -d --no-deps driver-platform` → 轮询容器健康状态 → 冒烟 `/health`、
`/metrics`、以及**带钥匙调用 `search_database`**（用演示库，验证「起来就能查」这条链路）→
`if: always()` 收尾 `docker compose down -v`。CI 里不起 Prometheus / Grafana：拉两个第三方镜像
对冒烟没有增益，纯粹花时间。

## 非目标（本阶段明确不做）

- 构建并推送镜像到镜像仓库、镜像 tag 与语义化版本策略（包版本仍是 `0.0.0`）；
- K8s / Helm 清单、Ingress、服务网格；
- 多副本、负载均衡、滚动更新、就绪探针（阶段 8）；
- Secrets 管理（compose 里的钥匙是**演示明文**；生产用 secrets / 环境注入）；
- TLS 终止与反向代理（本机演示是明文 HTTP）；
- 数据备份/恢复、卷迁移、异地部署；
- 容器加固（非 root 用户、只读根文件系统、`cap_drop`、资源限额）（阶段 8）；
- 告警规则与 Alertmanager（阶段 5 只做看板，不做告警）。

## 已知限制

1. **单副本**：`driver-platform` 只有一个实例，重启期间不可用（阶段 8 的滚动更新要解决的就是它）。
2. **容器以 root 运行、无资源限额**、无只读根文件系统——加固是阶段 8 的内容。
3. **第三方镜像钉的是 amd64 平台摘要**（本机与 CI 都是 amd64）。换 arm64 需要重新解析摘要，
   否则 `docker compose up` 会报找不到清单。
4. **基础镜像用标签而不是摘要**：`python:3.14-slim` 会随上游变化，同一份 compose 隔一段时间
   构建可能得到不同的基础层（这是拿可复现性换安全补丁，见选型理由）。
5. **compose 里的演示钥匙是明文**，且 `admin-key:plugin_admin` 能让持钥者注册远程插件
   （前提是 `DRIVER_REMOTE_PLUGIN_ENABLED=1`，compose 没开）。
6. **`/metrics` 无鉴权**并直接暴露在宿主 8000 端口（阶段 5 的设计，容器化不改变它）。
7. **Prometheus 只在平台健康后才启动抓取**：平台启动前的那段窗口没有样本。
8. **Grafana 首次启动慢**：本机在 WSL 卷上跑 SQLite 迁移需要数分钟（阶段 5 实测 813 次迁移
   才走完），期间 3000 端口连不上；数据卷建好之后重启很快。这是本机文件系统现象，与镜像无关。
9. **`docker compose down -v` 会删除三个数据卷**（演示库、Prometheus TSDB、Grafana 数据），
   要保留就先 `docker compose down`（不带 `-v`）。
10. **演示库种子只认 SQLite**：换别的数据库要自己准备数据与插件配置。
11. **provisioned 面板不允许 UI 修改**（`allowUiUpdates=false`）：容器里改的改动不会持久化。
12. **Grafana 自带插件的自动更新被关掉**（`GF_PLUGINS_PREINSTALL_DISABLED` /
    `GF_PLUGINS_PREINSTALL_AUTO_UPDATE`）：升级 Grafana 只能改 compose 里的镜像摘要，
    不能靠运行时自动更新——原因见验收证据第 6 条（离线时下载失败会连带注销 prometheus 数据源，
    面板随之「Datasource not found」）。

## 验收证据

### 1. 门禁（本机，2026-10-01）

| 命令 | 结果 |
|---|---|
| `ruff check .` | `All checks passed!`（exit 0） |
| `ruff format --check src tests deploy examples` | `58 files already formatted`（exit 0） |
| `mypy` | `Success: no issues found in 34 source files` |
| `pytest -q -p no:cacheprovider --cov=driver_platform --junitxml=…` | exit 0，**256 tests / 0 failures / 0 errors / 0 skipped**，覆盖率 `TOTAL 1248 68 95%` |

> 用例数从 junit XML 读：这个终端里 pytest 9.1.1 不打印「N passed」汇总行。
> `deploy/` 下的脚本不在覆盖率统计范围（`--cov=driver_platform` 只量 `src/`），
> 但它们由 `tests/test_deploy_config.py` 直接调用（种子幂等、入口脚本先种库再 `exec`）。

### 2. compose 起全平台（本机 Docker Desktop，engine 29.8.1 / compose v5.5.1）

- `docker compose config --quiet` → exit 0；
  `docker compose build driver-platform` → exit 0（`naming to docker.io/library/driver-platform:0.0.0`，镜像 **241 MB**）；
  `docker compose up -d` → exit 0。
- `docker compose ps`：

  ```
  driver-platform | driver-platform:0.0.0                  | Up (healthy)
  grafana         | grafana/grafana@sha256:b28bae15e2…      | Up
  prometheus      | prom/prometheus@sha256:efd719c99d…      | Up
  ```

- 命名卷里确实有演示库（把探针脚本 `cp` 进容器后执行）：
  `tables = ['customers', 'orders']`、`customers rows = 4`、`file exists = True`
  —— 入口脚本在首次启动时按 `DRIVER_SEED_DEMO_DB=1` 种好了数据。
- 宿主端口：8000（平台）、9090（Prometheus）、3000（Grafana）。

### 3. 端到端调用（宿主 `curl`，逐字响应）

| 请求 | 响应 |
|---|---|
| `GET /health` | 200 `{"status":"ok"}` |
| `GET /v1/tools`（`X-API-Key: read-key`） | 200，`echo` 与 `search_database` 两份完整 manifest |
| `POST /v1/tools/execute`（`read-key`，`{"tool":"search_database","arguments":{"table":"customers","filters":{"city":"上海"},"limit":2}}`） | 200 `{"tool":"search_database","contract_version":"1.0","result":{"table":"customers","columns":["id","name","city","plan"],"rows":[{"id":1,"name":"Acme 制造","city":"上海","plan":"pro"},{"id":4,"name":"海纳咨询","city":"上海","plan":"basic"}],"row_count":2,"total":2,"truncated":false}}` |
| 同上但不带钥匙 | **401**（`DRIVER_AUTH_MODE=required`） |

`/metrics` 里的样本：`driver_http_requests_total{method="GET",route="/health",status="200"} 3.0`、
`{method="POST",route="/v1/tools/execute",status="200"} 1.0`、`{…,status="401"} 1.0`、
`driver_tool_calls_total{outcome="success",tool="search_database"} 1.0`。

### 4. 观测三条线（Prometheus / Grafana / 日志）

- **Prometheus 抓到了**：`/api/v1/targets` → `http://driver-platform:8000/metrics`，`health=up`、`lastError=""`
  （服务名解析、compose 网络、5 秒抓取周期都对）。
- **Prometheus 里有数据**：`/api/v1/query?query=driver_tool_calls_total` →
  `{__name__="driver_tool_calls_total", instance="compose", job="driver-platform", outcome="success", tool="search_database"} 1`。
- **Grafana 自动装配**：`/api/health` → `{"database":"ok","version":"13.2.3",…}`；
  `/api/datasources` → `{"uid":"prom-main","name":"Prometheus","url":"http://prometheus:9090","isDefault":true,"readOnly":true}`
  （`readOnly=true` 说明它来自 provisioning，不是手工建的）；
  `/api/dashboards/uid/driver-platform-overview` → `panels=4`，四个面板的 `datasource.uid` 全是 `prom-main`，
  表达式与 `docs/observability/grafana-dashboard.json` 逐字一致。
- **面板真的能取数**：经 Grafana 数据源代理查 `driver_tool_calls_total` 返回
  `{instance="compose", job="driver-platform", outcome="success", tool="search_database"} 1`；
  面板走的那条 `/api/ds/query`（instant 查询）返回 `resultType=vector`、标签
  `{outcome="success", tool="search_database"}` 的长表数据。
- **日志能追**：容器日志是阶段 5 的 JSON 契约，逐行可读——
  `{"event": "http_request", "method": "GET", "path": "/metrics", "status": 200, "duration_ms": 1.009}`
  与 `{"logger": "driver_platform.tools", "msg": "tool_call", "request_id": "57588f8f0ba34ddb80b94cf91c8447f7", "event": "tool_call", "tool": "search_database", "outcome": "success", "duration_ms": 0.409}`；
  抓 `/metrics` 的来源地址是 `172.18.0.3`（Prometheus 容器），证明抓取发生在 compose 网络内而不是宿主转发。

### 5. CI

`deploy` 任务里那串命令在本机逐步跑过（`config` → `build` → `up -d --no-deps` → 轮询健康 →
`/health` + `/metrics` + 带钥匙调 `search_database` → `down -v`），与工作流里写的一致；
CI 上的实际结果见 GitHub Actions 页面（本机不是 runner）。

### 6. 本阶段自己踩出并修掉的真实缺陷

**Grafana 13 的自带插件自动更新，在离线网络下会把 prometheus 数据源注销掉。**
证据链（全部来自容器日志与本机请求，不是推测）：

```
logger=plugin.backgroundinstaller level=info  msg="Installing plugin" pluginId=prometheus
logger=plugin.installer            level=info  msg="Updating plugin" pluginId=prometheus from=13.2.1 to=13.2.2
logger=plugin.prometheus           level=info  msg="plugin process exited"
                                   plugin=/usr/share/grafana/data/plugins-bundled/prometheus/gpx_…
logger=plugin.backgroundinstaller level=error msg="Failed to install plugin" pluginId=prometheus
                                   error="failed to download plugin archive: Get
                                   \"https://storage.googleapis.com/grafana-plugins-catalog/prometheus/release/13.2.2/prometheus-13.2.2.linux-amd64.zip\": EOF"
```

于是面板那条路直接失败：`POST /api/ds/query` → `{"statusCode":404,"messageId":"plugin.notRegistered"}`，
数据源代理 → `{"message":"Unable to find datasource plugin"}`；而**平台侧一切正常**
（Prometheus 抓取照旧 `up`、指标照旧有数据）——这正是「静态配置看着都对、界面上却没数据」
的典型故障，只有真起一次全栈才会暴露。

修法：关掉预装与自动更新，重建容器。

```yaml
      GF_PLUGINS_PREINSTALL_DISABLED: "true"
      GF_PLUGINS_PREINSTALL_AUTO_UPDATE: "false"
```

修复后：日志里再无 `backgroundinstaller` 行（不再尝试外呼下载），`/api/ds/query` 返回数据，
数据源代理查询返回 `driver_tool_calls_total` 的向量。`tests/test_deploy_config.py` 新增
`test_grafana_does_not_auto_update_its_bundled_plugins` 守住这两个开关。

### 7. 与代码无关的本机环境现象

- Grafana **首次**启动在 WSL 卷上跑 SQLite 迁移极慢：本机第一次等到 `/api/health` 返回 200
  用了约 **6.5 分钟**（轮询 40 次 × 10 秒才成功）；数据卷建好之后 `--force-recreate`
  只用约 **20 秒**。这是本机文件系统现象（阶段 5 手工起容器时也遇到过），与镜像和配置无关。
- `docker buildx imagetools inspect` 取不到第三方镜像的**多架构 index 摘要**：它绕过 daemon 的
  镜像加速直连 `registry-1.docker.io`，本机被重置/超时。所以 compose 里钉的是本地
  `docker images --digests` 给出的摘要（已在[环境与工具手册](../environment.md) §2 记录）。
