# 阶段 5：可观测性（指标 + 结构化日志 + request_id 贯穿）

> 目标：Grafana 能看 QPS/延迟/错误率；日志可按 request_id 追全链路。状态：已完成（验收证据见文末）。

## 选型理由

**为什么是 `prometheus-client`（进程内暴露器），而不是 OpenTelemetry SDK + Collector**：本阶段的验收对象只有三条线——QPS、延迟、错误率，而拉取模型（Prometheus 定时 scrape `/metrics`）用一个纯 Python 依赖就能闭环：不需要 collector、不需要 exporter 管道、不需要新增 OTLP 端口。OTel 回答的是**跨进程追踪**（谁调了谁、span 树、采样），而这里要回答的是**单进程内的三条线 + 一条 request_id**；先上 OTel 等于为了看三条线先运营一套遥测后端。将来真要做分布式追踪，`observability/metrics.py` 是唯一改造点（埋点调用点不变），并且 Prometheus 文本格式可以被 OTel 桥接（`prometheusremotewrite`），不是死路。

**为什么指标默认关、而 request_id 与日志默认开**：原则「特性开关默认关」约束的是**暴露面**。`/metrics` 是一个新增的、无鉴权的端点，默认打开就是白送一个面；所以 `DRIVER_METRICS_ENABLED=0`，关掉时端点**根本不存在**（404），埋点空转（no-op，不产生序列）。而 request_id 与结构化日志不新增端点、不改任何语义（只在响应上多一个 `X-Request-ID` 头和一个日志字段），零成本换全链路可追，所以常开。同理 `DRIVER_LOG_FORMAT` 默认 `text`（人读、不打扰本地开发），要 JSON 行日志必须显式写 `json`。

**为什么埋点放在 `tools/executor.py`，而不是各协议适配器**：阶段 3 已经把「成功在同一处、失败也在同一处」钉死成规矩（HTTP 与 MCP 共用 `execute_tool`）。指标若写在适配器里，迟早出现「HTTP 计数了、MCP 没计数」这类最难发现的不一致——而且是**静默**不一致。写进执行管线后，工具指标天然覆盖 HTTP、MCP，以及阶段 6 的远程插件，不需要再改一行。

**为什么自定义 ASGI 中间件，而不用 `BaseHTTPMiddleware`**：Starlette 的 `BaseHTTPMiddleware` 每个请求再包一层 anyio task，`contextvars` 的传播语义随之改变（request_id 会丢或串），而且它拿到的是包装后的 response，看不到 `http.response.start` 的原始消息。纯 ASGI 中间件能在**响应头发出前**改写 header、在那一刻读到真实 status（含 404/500），并保证错误响应也带 `X-Request-ID`。

**为什么 `route` 标签用路由模板而不是原始路径**：`/v1/tools/{name}` 的原始路径会把每个工具名变成一条时间序列，基数随口径膨胀——这是 Prometheus 最经典的翻车方式。用 `scope["route"].path`（模板）做标签，未匹配路径统一归 `unmatched`。`tool` 标签同理只取注册表里的工具名。

**为什么错误信封里放 `request_id`**：HTTP 侧有响应头可以带，但 **stdio MCP 没有响应头**——它的失败只能出现在 `CallToolResult` 里。如果不写进载荷，MCP 客户端拿到一个失败就再也无法与服务端日志对上号，「按 request_id 追全链路」在 MCP 这条路径上直接断掉。所以 `request_id` 作为**可选字段**进入 `error` 信封（有请求上下文才出现），`code`/`message`/`details` 的语义与形状一律不变，属于兼容扩展。

**为什么日志只挂在 `driver_platform` 这一支 logger 上**：不碰 root logger，就不会和 uvicorn、pytest 的 handler 打架（pytest 的 `caplog` 仍能收到我们的日志，因为 `propagate` 保持为真）。JSON 行日志写 **stderr** ——stdio MCP 的 stdout 是协议通道，一个字节都不能混入日志。

## 模块设计

```
src/driver_platform/
  config.py                      + DRIVER_METRICS_ENABLED / DRIVER_LOG_LEVEL / DRIVER_LOG_FORMAT（非法值 __post_init__ 即失败）
  observability/context.py       request_id 的 ContextVar：生成 / 校验 / 读取（零依赖）
  observability/metrics.py       Metrics：HTTP 与工具指标 + track_tool_call()（不认识 FastAPI）
  observability/logging.py       configure_logging() + JsonFormatter / KeyValueFormatter
  observability/middleware.py    ObservabilityMiddleware（纯 ASGI：request_id → 响应头、访问日志、HTTP 指标）
  observability/routes.py        GET /metrics（仅开关打开时被 main.py 挂载）
  main.py                        配置日志 + 挂中间件 + 条件挂 /metrics
  tools/executor.py              执行管线埋点（HTTP / MCP / 远程插件共用同一条）
  protocols/mcp.py               tools/call 每次调用生成 request_id（stdio 没有请求头）
  errors.py                      to_payload() 附带 request_id（有上下文才出现）
```

依赖方向是单向的：`observability/` 只认识标准库与 `prometheus_client`，不认识 FastAPI、不认识任何工具、不认识插件；`tools/executor.py` 只 import `observability.metrics`；`errors.py` 只 import `observability.context`（零依赖）。所以「换掉 Prometheus」或「换成 OTel」都只影响 `observability/` 内部。

## 接口定义

### 环境变量

| 变量 | 默认 | 取值 | 语义 |
| --- | --- | --- | --- |
| `DRIVER_METRICS_ENABLED` | `0` | `0` / `1`（也接受 `true`/`false`/`yes`/`on`/`off`） | 打开 `/metrics` 与指标采集；关闭时端点不存在、埋点空转 |
| `DRIVER_LOG_LEVEL` | `INFO` | 标准库级别名（`DEBUG`…`CRITICAL`） | 只设置 `driver_platform` 这一支 logger 的级别；非法值启动即失败 |
| `DRIVER_LOG_FORMAT` | `text` | `text` / `json` | `json`：每行一个 JSON 对象（stderr）；非法值启动即失败 |

### `GET /metrics`（仅当 `DRIVER_METRICS_ENABLED=1`）

```
Content-Type: text/plain; version=0.0.4; charset=utf-8
```

与 `/health` 一样**不要求凭证**：抓取方是集群内的 Prometheus，给它配一把会轮换的 API Key 只会让人放弃抓取。边界就是它的开关：**默认关**，要开是显式动作，且文档明说「开了就是公开的」。

| 指标 | 类型 | 标签 | 语义 |
| --- | --- | --- | --- |
| `driver_http_requests_total` | Counter | `method` / `route` / `status` | HTTP 请求数（QPS 与错误率的来源） |
| `driver_http_request_duration_seconds` | Histogram | `method` / `route` | HTTP 端到端延迟（含执行时间） |
| `driver_tool_calls_total` | Counter | `tool` / `outcome` | 工具调用数（含 MCP 与远程插件） |
| `driver_tool_duration_seconds` | Histogram | `tool` / `outcome` | 工具执行管线耗时 |

`outcome` 取值即执行管线的稳定错误码，外加成功：`success` / `permission_denied` / `invalid_arguments` / `tool_not_found` / `tool_execution_failed` / `internal_error`。它和 `error.code` 是同一套词汇——**能按 code 分支的地方就能按 outcome 建面板**。

### 响应头 `X-Request-ID`

所有 HTTP 响应都带（含 401 / 403 / 404 / 422 / 500）。客户端可以自带，服务端接受 `^[A-Za-z0-9._-]{1,64}$` 的取值并原样回显；缺失或非法（带换行、超长）一律替换为服务端生成的 32 位十六进制——**客户端永远无法用 header 注入日志内容**。

### 日志字段契约（`json` 格式，每行一个对象）

| 字段 | 何时出现 | 说明 |
| --- | --- | --- |
| `ts` | 总是 | ISO 8601（UTC，毫秒精度，`Z` 结尾） |
| `level` / `logger` / `msg` | 总是 | 标准库同名字段 |
| `request_id` | 有请求/调用上下文时 | 与响应头、错误信封中的值一致 |
| `event` | 埋点日志 | `http_request` / `tool_call` |
| `method` `path` `status` `duration_ms` | `event=http_request` | `path` 是**路由模板**（原始路径不入日志，避免把参数写进日志） |
| `tool` `outcome` `duration_ms` `error_code` | `event=tool_call` | 失败时 `error_code` 与信封里的 `code` 一致 |

`text` 格式为 `ts level logger msg key=value ...`（同样带 `request_id`），便于本地 `grep`。

### 示例

```bash
DRIVER_METRICS_ENABLED=1 DRIVER_LOG_FORMAT=json uvicorn driver_platform.main:app --port 8000

curl -s -D - localhost:8000/health -o /dev/null | grep -i x-request-id    # X-Request-ID: 3f2c...
curl -s localhost:8000/metrics | grep driver_http_requests_total
```

Grafana 三条线对应的 PromQL：

```promql
# QPS
sum(rate(driver_http_requests_total[1m]))
# P95 延迟（按路由）
histogram_quantile(0.95, sum by (le, route) (rate(driver_http_request_duration_seconds_bucket[5m])))
# 错误率（5xx 占比）；`or vector(0)` 让「一条 5xx 都没有」显示成 0 而不是 No data
sum(rate(driver_http_requests_total{status=~"5.."}[5m]))
  / sum(rate(driver_http_requests_total[5m])) or vector(0)
```

## 关键代码

```python
# observability/context.py —— 相关性只有一个来源，用 ContextVar 而不是 threading.local
request_id_var: ContextVar[str | None] = ContextVar("driver_request_id", default=None)

def ensure_request_id(candidate: str | None = None) -> str:
    """合法就沿用（客户端自带），否则生成；并写入当前上下文的 ContextVar。"""
    value = candidate if candidate and _VALID.match(candidate) else new_request_id()
    request_id_var.set(value)
    return value

# observability/middleware.py —— 纯 ASGI：只在真实响应头发出前改它
async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
        return await self.app(scope, receive, send)
    ...
    async def send_wrapper(message):
        if message["type"] == "http.response.start":
            message.setdefault("headers", []).append(
                (b"x-request-id", request_id.encode("ascii"))
            )
        ...
```

```python
# tools/executor.py —— 埋点写在这一条管线上，HTTP 与 MCP 自动一起被计数
with METRICS.track_tool_call(tool.name):
    ...  # 校验 → 执行 → 序列化；异常按 error.code 记 outcome 后原样抛出
```

```python
# errors.py —— MCP 没有响应头，所以相关性必须进信封（可选字段，形状不变）
def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
    payload = {"error": {"code": self.code, "message": self.message, "details": self.details}}
    resolved = current_request_id() if request_id is None else request_id
    if resolved is not None:
        payload["error"]["request_id"] = resolved
    return payload
```

## 测试与部署方案

新增测试（`pytest -q`）：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_observability_context.py` | 生成值格式、合法自带值沿用、非法值（换行/超长/空）替换、上下文默认 `None` |
| `tests/test_metrics.py` | 关闭时 `/metrics` 404、打开时返回 Prometheus 文本且含指标名；计数随请求增长；Histogram 有 `_bucket/_sum/_count`；工具埋点按 `outcome` 分桶；`Metrics` 实例彼此隔离（私有 registry） |
| `tests/test_observability_logging.py` | JSON formatter 字段与 `ts` 形状、`request_id` 注入、`text` 格式 `key=value`、`configure_logging` 幂等（不叠加 handler）、非法级别/格式 `invalid_configuration` |
| `tests/test_request_id.py` | 所有响应带 `X-Request-ID`、自带值回显、错误响应（401/404/422）也带、信封内 `request_id` 与响应头一致、日志里能按 request_id 找到该请求 |
| `tests/test_executor_metrics.py` | 成功 → `outcome="success"`；越权 → `permission_denied`；非法入参 → `invalid_arguments`；未知工具 → `tool_not_found`；工具抛错 → `tool_execution_failed`；HTTP 与 MCP 共用管线因此都计数 |

部署（本阶段本机验证，阶段 7 收进 compose）：

```bash
python -m uvicorn driver_platform.main:app --host 0.0.0.0 --port 8000   # DRIVER_METRICS_ENABLED=1
docker run -d --name driver-prom -p 9090:9090 \
  -v "$PWD/docs/observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro" prom/prometheus
docker run -d --name driver-grafana -p 3000:3000 grafana/grafana
```

`docs/observability/prometheus.yml` 抓取 `host.docker.internal:8000`，`docs/observability/grafana-dashboard.json` 是三条线（QPS / P95 延迟 / 错误率）的面板，导入即用。**uvicorn 必须绑 `0.0.0.0`**：容器里的 Prometheus 从宿主侧进不来 `127.0.0.1`（本机实测踩过，绑回环时 target 一直 down）。

## 非目标（本阶段明确不做）

- **分布式追踪**：不接 OpenTelemetry SDK、不导出 span、不做采样；跨进程链路（Agent → 平台 → 数据库）留给真正需要的时候。
- **日志聚合与检索**：不引 Loki/ELK，只保证「一行一个 JSON 对象 + request_id」这个**可被采集**的形状。
- **告警**：不做 Alertmanager 规则，本阶段只把线画出来。
- **`/metrics` 的鉴权与独立端口**：靠默认关 + 网络边界（阶段 7 的 compose 不把它映射到公网）。
- **多进程指标**：不启用 `prometheus_client` 的多进程模式（`MULTIPROC_DIR`），阶段 8 多副本时由 Prometheus 侧聚合各副本。
- **接管 uvicorn 访问日志**：uvicorn 自己的日志保持原样，我们的结构化日志是**另一条**（`driver_platform` 支）。

## 已知限制

- 直方图使用默认桶（5 ms–10 s）：SQLite 上的慢查询会落进 `+Inf` 桶，无法分辨「到底多慢」；要更细的分布需自定义桶（`observability/metrics.py` 一处改动）。
- 访问日志不记录请求体与响应体（避免把 SQL 参数与业务数据写进日志），因此排障时只有「谁调了哪个工具、多久、什么结果」这一层。
- `X-Request-ID` 只保证**本进程内**贯穿；调用方若不透传，跨服务的相关性不做保证（没有 traceparent）。
- 指标是**进程内**的：重启即归零，历史由 Prometheus 保存，服务本身不落盘。
- 关闭开关时 `/metrics` 返回 404（而不是空文本）：这是刻意的——不存在的端点比一个空端点更不容易被误认为「已开启」。
- **没被错误处理器接住的异常**（框架 500）由最外层 `ServerErrorMiddleware` 直接生成，那个响应不经过本中间件，所以它**没有** `X-Request-ID` 头；但请求已经按 `status=500` 记进指标，也留下了带 `request_id` 与 traceback 的 `http_request` 日志行。要拿号请查日志，别指望 500 响应头。

## 验收证据

本机实测（2026-10-01，Windows + Python 3.14.7；uvicorn 以 `--host 0.0.0.0 --port 8000` 启动，`DRIVER_METRICS_ENABLED=1 DRIVER_LOG_FORMAT=json`）：

**1. 门禁全绿**

| 命令 | 结果 |
|---|---|
| `pytest -q -p no:cacheprovider --cov=driver_platform --cov-report=term-missing:skip-covered` | **167 passed**，覆盖率 **94%**（1021 语句 / 64 未覆盖） |
| `ruff check .` | All checks passed |
| `ruff format --check src tests` | 51 files already formatted |
| `mypy src` | Success: no issues found in 33 source files |
| `bandit -r src -q` | exit 0（仅 `plugins/sqlite.py` 的 B608 `# nosec` 提示） |

**2. `/metrics` 与 request_id（真实响应）**

- 开关开：`GET /metrics` → 200 `text/plain; version=0.0.4`，样本行含 `driver_http_requests_total{method="GET",route="/health",status="200"} 5.0`、`{method="GET",route="unmatched",status="404"} 1.0`（乱路径确实归 `unmatched`，原始路径没进标签）；开关关 → 404。
- 工具 404 的信封带号：`{"error":{"code":"tool_not_found","message":"未注册的工具：ghost","details":null,"request_id":"6d3e5bf5f0f54d1d9ecd1dae7c985e63"}}`，与响应头 `X-Request-ID` 同值（`tests/test_request_id.py` 钉住）。
- 传 `X-Request-ID: trace-demo.1` 原样回显；不传则由服务端发 32 位 hex；MCP 侧每次 `tools/call` 服务端发新号（`tests/test_mcp_server.py` 断言两次调用号不同）。

**3. Prometheus 抓取与查询（9090）**

- `GET /api/v1/targets` → `http://host.docker.internal:8000/metrics`，`health=up`、`lastError=''`。
- `driver_http_requests_total` 返回 7 条序列；`sum by (tool, outcome) (driver_tool_calls_total)` → `success/echo 2`、`tool_not_found/unknown 1`、`invalid_arguments/echo 1` —— outcome 与对外错误码是同一套词汇，能直接对着界面讲。
- P95（`[2m]` 窗口）→ `/metrics` `0.00475`，其余路由 `NaN`（窗口内没有观测，属正常）。

**4. Grafana 三条线（3000，grafana/grafana 13.2.3）**

- 数据源 `prom-main`（`http://host.docker.internal:9090`，默认）创建成功；面板 `驱动平台 · 阶段 5 概览` 导入成功：uid `driver-platform-overview`、url `/d/driver-platform-overview/f04b765`、version 2、4 个面板（QPS / P95 延迟（按路由）/ 错误率（5xx 占比）/ 工具调用（按工具与结果））。
- 经 Grafana **自己的数据源代理**执行四个面板的表达式，全部取到数：QPS `0.2545593396003418`；P95 按路由 `/v1/tools/execute` `0.00475`（无观测的路由为 NaN）；错误率 `0`；工具调用 `success/echo 0.00678`、`tool_not_found/unknown 0.00339`、`invalid_arguments/echo 0`。
- 实测发现并修掉一处：错误率表达式不加 `or vector(0)` 时，「一条 5xx 都没有」会让整个比值成为空结果，面板显示 No data；加上之后健康系统显示 0。

**5. 与代码无关的本机环境现象**

- Grafana 13.2.3 在本机第一次起容器时迁移了 813 次才结束，却**始终没有监听 3000**（`/api/health` 无响应、容器内 `netstat -ltn` 无监听）；删容器重建（挂 named volume、关掉 analytics/news 外呼）后同样很慢，约 4–5 分钟才起来。属本机 WSL vhdx 上 sqlite 迁移极慢，与平台代码无关。
