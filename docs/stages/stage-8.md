# 阶段 8：生产加固（多副本 + 滚动更新不中断 + 旧版 Agent 兼容）

> 验收目标：多副本、滚动更新不中断、旧版 Agent 兼容。

阶段 7 证明了「一条命令起全平台」，但那份 compose 是**单副本、停一个就没有服务**的形态。
本阶段补三件生产必备的东西，并且每一件都要能**演示成事实**，而不是写进文档的形容词：

1. 多副本：同一份代码起 N 个实例，前面有一个稳定入口；
2. 滚动更新不中断：替换副本期间持续打流量，失败数为 0；
3. 旧版 Agent 兼容：老客户端不因平台升级而失效，且这条承诺被测试钉住。

## 选型理由

- **多副本用 Compose `--scale` + nginx 前置，而不是 K8s / Helm**：本阶段要证明的是「多副本能跑、
  换副本不掉请求」，不是「引入一套编排层」。Compose 已经给了需要的一切原语——服务名 DNS（返回
  **全部**副本 IP）、健康门（`service_healthy`）、`--scale`；nginx 只要用 `resolver 127.0.0.11` +
  变量形式的 `proxy_pass`，就会按 Docker DNS 的解析结果轮询到所有副本。反过来，换 K8s 会把本阶段
  唯一的对照物（阶段 7 那份 compose 文件）丢掉，而且集群本身要不要维护是另一个层级的问题。
- **前置网关用 `nginx:1.27-alpine` 钉摘要**：与 Prometheus / Grafana 同一套规矩——第三方镜像要
  「同一份 compose 起同一个镜像」。nginx 的（`error timeout http_502/503/504 non_idempotent`）重试
  正是「副本正在退出」这一瞬间不掉请求的兜底机制，所以它是本阶段的功能组件，不是可选项。
- **优雅停机靠 uvicorn 的 `--timeout-graceful-shutdown` + FastAPI lifespan，不自己写信号处理**：
  阶段 7 的 `os.execvp` 已经让 uvicorn 是 PID 1，`docker stop` 的 SIGTERM 直达应用。缺的两件事
  uvicorn 都提供——停止接受新连接、等在途请求收尾；我们只需要在 lifespan 的退出阶段关掉插件资源
  （SQLite 连接、将来的连接池）。自己注册 signal handler 会和 uvicorn 的停机流程抢同一件事。
- **滚动更新用脚本（`deploy/rollout.py`）而不是 `docker compose up -d` 的默认行为**：Compose 在
  非 swarm 模式下没有原生 rolling（`deploy.replicas` 直接被忽略，`up -d` 对已存在容器是先停后起）。
  验收要求「不中断」是**可测量的**，所以脚本必须显式地「停一个、补一个」，并用真实流量把「中断」
  变成数字（0 失败）。脚本用 Python 而不是 shell：本机默认外壳是 PowerShell 5.1，没有 bash 保证；
  Python 是仓库已有的依赖，`subprocess` 调 docker 在 Windows / Linux 上都一样。
- **探针用纯标准库（`deploy/traffic_probe.py`），不用 ab / hey / k6**：本机这些工具都没装，
  而验收需要的是「有没有掉请求」，不是压测报告。`urllib` + `ThreadPoolExecutor` 六十行就够，
  零依赖、可读、能被 CI 静态校验。指标里能另外看到副本分布，不需要探针自己统计。
- **旧版 Agent 兼容靠「把现状钉成测试 + 写下策略」，而不是加一层版本协商中间件**：现在
  `POST /v1/tools/execute` 的请求体与工具入参模型的默认行为就是 `extra='ignore'`（宽容），
  响应固定三键 `tool` / `contract_version` / `result`。真正的脆弱点是**没人知道这是承诺**——
  下一个人给某个入参模型加 `extra="forbid"`，兼容性就静默破了。于是：策略写进本文档与 README，
  行为用 `tests/test_compat.py` 钉住。中间件在这个规模上是纯粹的额外复杂度。
- **Prometheus 抓取从 `static_configs` 改成 `dns_sd_configs`**：副本数会变，副本 IP 也是动态的，
  而 `static_configs` 里写一个服务名只会固定解析结果。`dns_sd_configs` 每次刷新都把服务名解析成
  全部副本 IP，**一份配置同时服务单副本与多副本**，不必为 scale overlay 再维护第二份抓取配置。
- **CI 只做静态校验，不在 CI 里跑真滚动**：真滚动要 build 镜像、拉 nginx、起 3 个容器再加 60 秒
  探针，对「冒烟」的收益太小（阶段 7 的 deploy 任务已经证明镜像能起、链路能通）。真滚动的验收
  放在本机脚本里，而脚本本身就是可重复的验收——命令写进本文档，谁都能跑第二遍。
- **overlay 分层（`docker-compose.scale.yml`）而不是改写阶段 7 的 compose**：单副本仍是默认形态
  （本机开发、CI 冒烟都用它，已验收过的东西不该被顺手改掉）；多副本是**显式选择**
  （`-f docker-compose.yml -f docker-compose.scale.yml`）。Compose 的合并语义正好符合需要：
  覆盖 `ports`、追加一个 `gateway` 服务、其余原样继承。

## 模块设计

```
                     ┌──────────── 宿主 :8000（唯一入口）────────────┐
  Agent / MCP 客户端 ─┤ gateway  nginx@sha256:65645c…  （钉摘要）      │
                     └───────┬───────────────────────────┬──────────┘
                             │ resolver 127.0.0.11 valid=5s（请求时解析）
                    ┌────────┴─────────┐        ┌────────┴─────────┐
                    │ driver-platform  │        │ driver-platform  │   N 个副本
                    │ uvicorn + lifespan(host.close())             │   （--scale N）
                    └────────┬─────────┘        └────────┬─────────┘
                             │  Docker DNS: driver-platform → 全部副本 IP
                     prometheus（dns_sd_configs） ── 抓**每一副本**的 /metrics
```

- **对外只有一个入口**：scale overlay 把平台的宿主端口发布去掉（`ports: !override []`），
  否则 `--scale 2` 立刻端口冲突。宿主 `DRIVER_PORT` 改由 gateway 占用。
- **服务名不变**：副本仍叫 `driver-platform`，Docker DNS 才会把全部副本 IP 一起返回；
  Prometheus 配置因此不需要随副本数改。
- **单副本与多副本共用一份抓取配置**（`dns_sd_configs`），阶段 7 的本机配置
  （`docs/observability/prometheus.yml`）保持 `static_configs`——它抓的是宿主进程，没有服务名可解析。
- **退出路径**：SIGTERM → uvicorn 停收新连接 → 等在途请求（≤20s）→ lifespan 退出阶段
  `host.close()`（逆序 `unload()` 全部插件，尽力而为）→ 进程退出。
- **滚动替换**：`deploy/rollout.py` 逐个体执行「`docker stop`（优雅，20s 上限）→ `docker rm` →
  `docker compose … up -d --scale N` 补一个新副本 → 等健康」。任何时刻至少有 N−1 个副本在服务。

## 接口定义

### 平台侧：没有新增业务端点

本阶段**刻意不加** `/ready` 就绪探针（见非目标）。对外契约与阶段 4–7 完全一致：

| 端点 | 方法 | 变化 |
| --- | --- | --- |
| `/health` | GET | 无（仍是唯一公开端点；探针、健康门、滚动脚本都用它） |
| `/v1/tools`、`/v1/tools/execute` | GET / POST | 无 |
| `/v1/plugins`、`/v1/plugins/remote*` | GET / POST / DELETE | 无 |
| `/metrics` | GET | 标签里 `instance` 由 compose 提供（多副本时每副本一条序列） |
| MCP（stdio） | — | 无 |

进程生命周期契约（本阶段新增的**行为**契约）：

1. 收到 SIGTERM 后不再接受新连接；
2. 在途请求最多 20 秒收尾（`--timeout-graceful-shutdown 20`）；
3. 收尾阶段按装载的逆序卸载插件（`PluginHost.close()`），单个插件 `teardown()` 抛错不阻断其余；
4. 无论收尾是否异常，进程最终退出码为 0（`docker stop` 语义）。

### 契约兼容策略（1.x 版本线）

「旧版 Agent 兼容」的判据不是「能不能连上」，而是**平台的哪些改动会让老客户端失效**。
下表的「允许」项是当前实现的行为，由 `tests/test_compat.py` 钉住；「破坏性」项要动就必须升
`contract_version` 主版本（原则「插件契约版本化」的延长线）。

| 变更 | 是否允许 | 老客户端会怎样 |
| --- | --- | --- |
| 工具入参新增**可选**字段 | ✅ | 不传就用默认值 |
| 工具入参新增**必填**字段 | ❌ 破坏性 | 老客户端 422 `invalid_arguments` |
| 请求体顶层出现未知字段 | ✅ 忽略（现状） | 不影响 |
| `arguments` 里出现未知字段 | ✅ 忽略（现状） | 不影响（工具入参模型默认 `extra='ignore'`） |
| 收紧取值（如 `limit` 上限变小） | ❌ 破坏性 | 老客户端可能突然 422 |
| 响应新增字段 | ✅ | 忽略未识别字段即可 |
| 响应删字段 / 改语义 | ❌ 破坏性 | 解析失败或拿到错的值 |
| 错误信封新增 `details` / `request_id` | ✅ | 只读 `error.code` / `error.message` 就够 |
| 错误 `code` 改名 / 改 HTTP 状态码 | ❌ 破坏性 | 按 code 分支的老客户端走错路 |
| 工具从注册表消失（卸载插件） | ⚠️ 已知 | 404 `tool_not_found`——这是**功能**（阶段 6 的下线语义） |

响应形状（不变，测试钉住）：

- 成功：`200 {"tool": str, "contract_version": "1.0", "result": any}`
- 失败：`{"error": {"code": str, "message": str, "details"?: any, "request_id"?: str}}`

### 环境变量

本阶段**没有新增平台环境变量**（`config.py` 一行未改）。新增的只有部署与验收脚本的开关：

| 变量 | 默认 | 取值 | 语义 |
| --- | --- | --- | --- |
| `DRIVER_PORT` | `8000` | 端口 | 对外唯一入口。阶段 7 指向平台自身；scale overlay 下指向 gateway |
| `PROMETHEUS_PORT` / `GRAFANA_PORT` | `9090` / `3000` | 端口 | 阶段 7 起就有，本阶段未变 |
| `REPLICAS` | `2` | 整数 ≥ 2 | `deploy/rollout.py` 的副本数（`docker compose --scale`） |
| `PROBE_SECONDS` | `45` | 秒 | 滚动期间探针持续时间 |
| `API_KEY` | `read-key` | 字符串 | 探针调 `/v1/tools/execute` 时带的 `X-API-Key` |

（前三个是 compose 插值变量，后三个是脚本变量——都不进 `config.py`，也不属于平台配置。）

## 关键代码

### 1. 优雅停机：lifespan 关掉插件资源（`src/driver_platform/main.py`）

```python
@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """退出阶段收尾：容器 `docker stop` 时 SIGTERM 直达 uvicorn（阶段 7 的 os.execvp），
    uvicorn 负责「停收新连接 + 等在途请求」，这里只负责关掉插件资源。"""
    yield
    host: PluginHost = app.state.plugins
    logger.info("进程收尾：卸载 %d 个插件", len(host))
    host.close()
```

### 2. 停机窗口写在镜像里（`Dockerfile`）

```dockerfile
CMD ["uvicorn", "driver_platform.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "20"]
```

### 3. 动态解析副本 + 失败重试（`deploy/nginx/conf.d/default.conf`）

```nginx
resolver 127.0.0.11 valid=5s ipv6=off;   # Docker 内嵌 DNS：每次都拿到当时的全部副本 IP
location / {
    set $backend "driver-platform";
    proxy_pass http://$backend:8000;     # 变量形式的 proxy_pass 才会走 resolver（静态 upstream 只解析一次）
    proxy_next_upstream error timeout http_502 http_503 http_504 non_idempotent;
}
```

### 4. 滚动替换：停一个、补一个（`deploy/rollout.py`）

```python
def rolling_replace(replicas: int, wait_healthy: Callable[[], None]) -> list[str]:
    """逐个替换副本：任何时刻至少有 replicas-1 个在服务，gateway 不会把请求送到已停的容器。"""
    for container in current_containers():
        run(["docker", "stop", "--time", "25", container])   # 优雅停机（uvicorn 上限 20s）
        run(["docker", "rm", container])
        run(["docker", "compose", *COMPOSE_FILES, "up", "-d",
             "--no-deps", "--scale", str(replicas), "driver-platform"])
        wait_healthy()
```

### 5. 抓全部副本（`deploy/prometheus/prometheus.yml`）

```yaml
scrape_configs:
  - job_name: driver-platform
    metrics_path: /metrics
    dns_sd_configs:
      - names: ["driver-platform"]
        type: A
        port: 8000
        refresh_interval: 15s
```

## 测试与部署方案

### 单元测试（进 CI）

- `tests/test_compat.py`：把契约兼容策略的「允许」项钉成断言——
  顶层未知字段被忽略、`arguments` 未知字段被忽略、只给 `tool` 不给 `arguments` 也能走通、
  响应键集合固定为三键、错误信封至少含 `code`/`message`（`details`/`request_id` 可有可无）、
  已发布工具的 `contract_version` 仍是 `1.0`、`/v1/tools` 的 `input_schema` 是工具入参模型的超集。
- `tests/test_scale_config.py`：部署描述的静态校验——overlay 能解析且把平台端口 `!override` 掉、
  gateway 钉了摘要并挂载 nginx 配置、nginx 配置含 `resolver 127.0.0.11` 与变量式 `proxy_pass` 与
  `proxy_next_upstream`、`Dockerfile` 的 CMD 带 `--timeout-graceful-shutdown`、
  `deploy/prometheus/prometheus.yml` 用 `dns_sd_configs` 指向服务名 `driver-platform`、
  `main.py` 的 lifespan 在退出阶段调用 `host.close()`、两个脚本能被 `compile()`（语法门禁）。
- `tests/test_deploy_config.py`：把阶段 7 那条「两份抓取配置除 target 外全等」改成
  「compose 侧用 `dns_sd_configs` 抓服务名、宿主侧用 `static_configs` 抓 `host.docker.internal`」——
  两者刻意不同（服务名 vs 宿主进程），原来那条断言已不再表达真实意图。

### 本机验收（多副本 + 滚动，写进本阶段验收证据）

```powershell
# 1. 起 3 副本 + gateway（宿主 8000 由 gateway 占用）
docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --build --scale driver-platform=3
curl.exe -s http://127.0.0.1:8000/health

# 2. 看请求真的散到了不同副本：每个副本的 /metrics 各有一份计数
# 3. 滚动替换：一边打流量一边逐个换副本，断言 0 失败
python deploy/rollout.py --replicas 3 --seconds 60
```

判据：探针 `failed == 0`；`docker compose ps` 三个副本都 healthy；Prometheus 的
`count(up{job="driver-platform"} == 1)` 等于副本数；替换过程中 `/health` 的响应一直有人答。

### CI

`deploy` 任务追加静态校验（overlay `config --quiet`、两个脚本 `py_compile`），
真滚动不上 CI（理由见选型）。

## 非目标（本阶段明确不做）

- **`/ready` 就绪探针**：nginx 的 `proxy_next_upstream` 已经覆盖「副本正在退出」这一瞬；
  再加一个没人轮询的就绪端点就是死灵活性（接 K8s 时它的 `readinessProbe` 才有消费者，那时再补）。
- **K8s / Helm / 服务网格 / 滚动发布控制器**：本阶段用 compose 证明能力，不是引入编排层。
- **跨进程链路追踪（W3C `traceparent`）**：`request_id` 已能串起单进程全链路；跨进程传播要动
  中间件与所有适配器，属独立课题。
- **会话保持 / 亲和 / 限流 / 鉴权网关**：nginx 在这里只做「解析 + 转发 + 重试」，鉴权仍在平台内
  （阶段 4 的凭证与作用域），多一层网关鉴权会造出第二个真话来源。
- **多副本共享写型数据库**：SQLite 插件是只读打开（阶段 2），多副本共挂一份只读库是安全的；
  写型插件需要真正的共享存储与事务语义，不在本阶段。
- **镜像瘦身 / 非 root 运行 / 镜像签名**：属供应链加固，与「多副本、滚动、兼容」无关，见已知限制。

## 已知限制

1. **nginx 的解析有 5 秒窗口**（`resolver valid=5s`）：副本下线后最多 5 秒内仍可能被选中，
   由 `proxy_next_upstream` 重试兜底——客户端不报错，但会多一跳、多一次连接。
2. **重试对非幂等请求也开着**（`non_idempotent`）：当前工具全是只读查询（`echo` / `search_database`），
   重试安全。若将来加入写型工具，必须引入幂等键，否则一次重试可能写两遍。
3. **Compose 的 `--scale` 不是编排**：不会自动补回挂掉的副本、不保证分散到不同宿主、
   没有滚动策略与进度控制；`rollout.py` 只是把「逐个体替换」做成了可重复的脚本。
4. **单机多副本共享 `data` 卷**：当前只有只读的 SQLite 插件所以安全；任何写型插件在多副本下都不安全。
5. **探针只衡量成功率**：`traffic_probe.py` 报成功/失败与状态码分布，不衡量延迟分布、连接复用、
   长连接行为；要压测请另上专业工具。
6. **优雅停机上限 20 秒**：超过就被 uvicorn 强杀，在途请求会断——这是刻意的上限（不让一个卡住的
   请求拖住整个替换过程），不是缺陷。
7. **镜像仍以 root 运行、仍绑基础镜像标签、仍只验证过 amd64**：阶段 7 已记的限制延续，
   与多副本无关但同样不适合直接进生产。
8. **`/metrics` 仍无鉴权**：任何能连上入口的人都能抓指标（阶段 4 起就是如此）；scale 形态下它经
   gateway 暴露，生产应把 `/metrics` 限制在内部网络。
9. **滚动脚本不做版本并存的双写/流量着色**：无状态服务不需要，但也就意味着它验证不了
   「新旧版本同时在跑时的语义兼容」——那部分由契约兼容策略 + 测试覆盖。
10. **远程插件客户端会继承宿主机的代理设置**：`RemoteClient` 用默认的 `httpx` 行为（读环境变量与
    Windows 系统代理），所以在一台开着系统代理的机器上，访问 `127.0.0.1` 或内网地址的远端插件也可能
    走代理（测试里表现为 502，见验收证据第 6 节第 7 条）。这是刻意的「跟随环境」而不是 `trust_env=False`：
    有些部署确实要靠代理出网才能连上远端插件。生产若把远端插件放在私网，应显式给 `NO_PROXY`
    加回环与私网段，或给插件宿主单独配代理策略。

## 验收证据

### 1. 四道门禁（本机，提权会话）

| 命令 | 结果 |
| --- | --- |
| `ruff check .` | `All checks passed!`（exit 0） |
| `ruff format --check src tests deploy examples` | `63 files already formatted`（exit 0） |
| `mypy` | `Success: no issues found in 34 source files` |
| `pytest -q -p no:cacheprovider --cov=driver_platform --cov-report=term-missing:skip-covered` | exit 0，**276 passed / 0 failed / 0 error**，`TOTAL 1257 65 95%` |

新增用例分布：`tests/test_compat.py`（契约兼容 9）、`tests/test_lifecycle.py`（lifespan 收尾 2）、
`tests/test_scale_config.py`（部署描述静态校验 + 探针自测 12）；`tests/test_deploy_config.py` 改 1 增 1。

CI（提交 `894ff44`，[run 37953653712](https://github.com/qw2007/DRiver-api/actions/runs/37953653712)）：
`test`（ruff check → ruff format → mypy → pytest）与 `deploy`（compose 与 provisioning 校验 →
**新增的多副本 overlay / nginx -t / 脚本 py_compile 校验** → 构建 → 起服务 → 健康 → 两个冒烟）两个任务均 success。

### 2. 本机多副本 + 滚动替换（0 失败）

`.tmp/rolling.ps1` 是 `deploy/rollout.py` 的等价手工序列（为什么不用脚本见第 5 节）。
一次完整跑（脚本自身 `script-exit=0`）：

```
=== 0) 当前运行中的副本与镜像（旧版本） ===
old-image: sha256:fcb83fa59641…   ×3 全 healthy
=== 1) 构建新镜像（带构建戳，确保镜像 ID 变化） ===
build-stamp: 20261009-233344   new-image: fad8a0d26702   image-changed: True
=== 2) 经网关的连通性 ===
gateway /health:    {"status":"ok"}
gateway execute:    {"tool":"echo","contract_version":"1.0","result":{"echo":"rollout","times":1}}
=== 4) 逐个替换副本（stop --time 25 → rm → up -d --no-deps --no-recreate --scale driver-platform=3）===
  stop cbd54fdbbf36 → 新 ×3 healthy…（中间态：新 ×1 + 旧 ×2 → 新 ×2 + 旧 ×1 → 新 ×3）
  stop 5dde15dd133c → fad8a0d26702 ×2 + fcb83fa59641 ×1 全 healthy
  stop 2161b22b21af → fad8a0d26702 ×3     全 healthy
=== 5) 探针结果 ===
probe-total=86875 probe-failed=0
{"total": 86875, "ok": 86875, "failed": 0, "statuses": {"200": 86875}, "errors": {},
 "duration_ms": {"min": 1.863, "median": 3.090, "max": 2014.668}}
```

- 判据全部满足：探针 `failed == 0`；每次替换后 `docker inspect … .State.Health.Status` 都是三个
  `healthy`；整个 180 秒窗口内**新旧镜像同时在线**（这才是「滚动」而不是「先停后起」），
  经网关的 8.7 万次请求一次没掉。
- 上一次同序列跑（旧→新 `343c28c134b5` → `fcb83fa59641`）是 `total=91671 / failed=0 / statuses {"200": 91671}`。
- 探针是「经网关」的：请求打的是 `http://127.0.0.1:8000/…`（gateway 容器），不是某个副本，
  所以它衡量的是「客户端视角有没有断」，不是某个容器的自述。
- `image-changed: True` 靠构建戳（`--build-arg DRIVER_BUILD_STAMP=<时间戳>`）：同源重建命中构建缓存时
  镜像 ID 不变，光看 `docker compose build` 输出分不清新旧；这一步反过来说明**「替换」真的换了镜像**。

### 3. 抓全副本（Prometheus）

`deploy/prometheus/prometheus.yml` 改成 `dns_sd_configs` 后，让运行中的 Prometheus 重载
（`docker compose … kill -s HUP prometheus`），`GET http://127.0.0.1:9090/api/v1/targets` 返回：

```
targets-count=3
target=http://172.18.0.2:8000/metrics instance=172.18.0.2:8000 health=up lastError=''
target=http://172.18.0.6:8000/metrics instance=172.18.0.6:8000 health=up lastError=''
target=http://172.18.0.5:8000/metrics instance=172.18.0.5:8000 health=up lastError=''
# up{job="driver-platform"} → 3 条序列，值全为 1（instance 分别是三个副本的 IP）
```

即验收判据里的 `count(up{job="driver-platform"} == 1) == 副本数` 成立；Grafana 侧继续用阶段 5 的四个面板
（数据源 `prom-main`），多出来的 `instance` 维度让「哪个副本在答」可以直接 `sum by (instance)`。

### 4. 优雅停机（20 秒窗口真的被用上）

停一个副本（`docker stop --time 25`）后它自己的日志：

```
INFO:     Shutting down
INFO:     Waiting for application shutdown.
{"ts": "2026-10-09T15:38:30.559Z", "level": "INFO", "logger": "driver_platform.main",
 "msg": "进程收尾：卸载插件（逆序，逐个尽力而为）"}
INFO:     Application shutdown complete.
INFO:     Finished server process [1]
```

`docker stop` 随即返回（没等到 25 秒上限），说明 uvicorn 收到 SIGTERM 后**先停止接受新连接、再跑 lifespan
的退出阶段**（`PluginHost.close()` 逆序卸载插件、逐个尽力而为），最后正常退出——这正是
`--timeout-graceful-shutdown 20` 与 lifespan 的组合要的效果。

### 5. 与本机 DSH 会话有关的现象（不是产品缺陷）

- 本机会话里 **Python 子进程被沙箱限制**：`python deploy/rollout.py` 报
  `RuntimeError: 命令失败（exit 1）：docker compose … build driver-platform`，Docker 侧原文是
  `open <用户目录>\.docker\buildx\.lock: Access is denied.`；不构建时则报
  `permission denied while trying to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`。
  分步实验（pwsh → `docker compose build` 成功；pwsh → `cmd /c docker compose build` 成功；
  pwsh → `cmd /c echo x > …\.probe-write` 成功；pwsh → `.venv\Scripts\python.exe -c "open(path,'w')"` 被拒）
  表明限制只落在 **Python 子进程**上，`danger-full-access` 也解不开。
  所以第 2 节的证据由 PowerShell 等价序列产出，`deploy/rollout.py` 保持为普通环境/CI 里的自动化路径
  （它本身靠 `py_compile` + 静态校验守，见测试方案）。
- 本机 shell 是 **PowerShell 5.1**（不是 pwsh 7）：写出的 `.ps1` 必须补 BOM，否则中文按 GBK 解码 →
  `Missing closing '}' in statement block`；`$ErrorActionPreference = "Stop"` 会把 docker 写到 stderr 的
  正常进度行变成终止性错误；`Start-Process -PassThru` 读不到 `ExitCode`（一直为空）——所以脚本判据
  改成解析探针自己打印的 JSON 汇总，`ExitCode` 只作为参考信息打印。

### 6. 本阶段自己踩出并修掉的真实缺陷

1. `deploy/rollout.py` 的 `--scale 3` 是非法语法（docker 原文
   `invalid --scale option "3". Should be SERVICE=NUM`）→ 改成 `--scale driver-platform=<n>`。
2. `deploy/rollout.py` 的 `os.environ.get("DRIVER_PORT", 8000)` 默认值类型错（PLW1508）→
   `int(os.environ.get("DRIVER_PORT") or 8000)`。
3. `docker-compose.scale.yml` 的 gateway 镜像写成 `nginx:1.27-alpine@sha256:…`，仓库既有的
   `DIGEST_PATTERN` 不接受带 tag 的引用 → 改成纯摘要引用（与 prom/grafana 的钉法一致），CI 同步。
4. `tests/test_lifecycle.py` 原来断言退出后注册表为空 —— 但 `echo` 是**内置工具**、不归插件管，
   实际是 1 条；改为断言「插件工具被摘掉、内置工具还在」。（lifespan 本身没问题。）
5. `tests/test_scale_config.py` 的 `_load_module` 必须在 `exec_module` **之前**把模块登记进
   `sys.modules`，否则脚本里的 `@dataclass` 解析字符串注解时拿到 `None` →
   `AttributeError: 'NoneType' object has no attribute '__dict__'`。
6. nginx 断言 `"upstream " not in config` 被**注释里**的「静态 upstream」误伤 → 改成只看非注释行。
7. `tests/test_remote_api.py` 的 9 个用例在本机全 502：`httpx` 0.28 会读 **Windows 系统代理（注册表）**，
   本机 Clash 监听 `127.0.0.1:7897`，代理把回环临时端口也接走了（`urllib` 200 / `httpx.Client()` 502 /
   `httpx.Client(trust_env=False)` 200）。修法是在 `tests/conftest.py` 里补 `NO_PROXY` 回环豁免，
   不改产品行为（生产为什么不该一刀切关掉代理见已知限制第 10 条）。
8. Windows shell 会吃掉 `--execute` 里的内嵌双引号（JSON 到进程里变成 `{tool:echo}` →
   `json.decoder.JSONDecodeError`）→ 探针新增 `--execute-file`，验收脚本改走文件。
9. 「替换了但看不出换了」：同源重建命中构建缓存时镜像 ID 不变 → 加 `ARG DRIVER_BUILD_STAMP` / `LABEL`
   与 `tests/test_scale_config.py::test_image_carries_a_build_stamp`，让「新旧」在镜像层可核对。
