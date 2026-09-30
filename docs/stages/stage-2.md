# 阶段 2：插件系统 + 数据库插件

> 目标：工具列表出现 `search_database`；可注册/注销；结构化结果。状态：已完成。

## 选型理由

**为什么插件不是「往注册表里塞函数」**：平台要回答三个问题——这些工具是谁提供的、能不能整体下线、下线时资源怎么释放。所以插件是一个**有身份（`plugin_id`）、有版本、能装载/卸载**的单元，工具只是它对外暴露的产物。这个边界定清楚，阶段 6 的远程插件才只是换一个 `Plugin` 实现，而不是重写宿主。

**为什么 `search_database` 收结构化参数，而不是让 Agent 写 SQL**：中间层的价值是**约束**，不是转发。收 `{table, columns, filters, limit, offset}` 之后：表名/列名必须命中从库里读出来的真实 schema（拼不出不存在的标识符），值一律走占位符绑定（注入面为零），返回行数与截断标记确定（Agent 不需要猜）。代价是暂时没有 join / 聚合——这是明确的非目标，原始 SQL 通道要等阶段 4 的权限层来兜底，否则等于把数据库直接敞开给模型。

**为什么插件默认不启用（配置外部化 + 特性开关默认关）**：起服务不该偷偷连库。`DRIVER_PLUGINS` 决定装哪些插件，而启用了 `sqlite` 却不给 `DRIVER_SQLITE_PATH`、或文件不存在，**启动即失败**——不允许静默建一个空库让人查半天。

**为什么数据库先走 SQLite**：本机无 Docker，SQLite 零依赖、能进 CI、能真跑集成测试。PostgreSQL 复用同一套插件契约，等阶段 7 有 Compose 时再用 Testcontainers 补集成测试，插件契约不因此改动。

## 模块设计

```
src/driver_platform/
  config.py             Settings.from_env()：DRIVER_PLUGINS / DRIVER_SQLITE_PATH
  bootstrap.py          create_registry_and_host(settings)：装配内置工具 + 装载插件
  plugins/base.py       Plugin 契约 + PluginManifest（对外说明书）
  plugins/host.py       PluginHost：load / unload / manifests，失败回滚
  plugins/catalog.py    插件 id → 工厂（阶段 6 的远程插件在这里扩展）
  plugins/sqlite.py     SqlitePlugin + SearchDatabaseTool
  api/plugins.py        GET /v1/plugins
```

职责边界：`PluginHost` 只做生命周期与原子性，不碰 SQL；插件只描述「我能提供什么工具、怎么收尾」，不碰注册表；`ToolRegistry` 仍是进程内工具的唯一真相（阶段 1 的约定不变），阶段 2 只给它加一个 `unregister`。

**装载的原子性**：`load()` 先把工具逐个注册，任一冲突（例如插件工具名撞上内置 `echo`）就**回滚已注册的工具并调用 `teardown()`**，绝不留下装载一半的插件。报告冲突时把已回滚的事实一并说清。

**为什么注册/注销不做成 HTTP 接口**：`POST /v1/plugins` 的语义是「上传并加载一段代码」，那是一个远程代码执行面，必须等阶段 4 的鉴权与阶段 6 的远程插件契约来兜底。本阶段 HTTP 只暴露只读的 `GET /v1/plugins`，注册/注销走 Python API 并由测试验收。

## 接口定义

### 插件契约（Python）

```python
class Plugin(ABC):
    plugin_id: str
    version: str
    description: str

    @abstractmethod
    def tools(self) -> list[Tool]: ...

    def setup(self) -> None: ...      # 建连接、校验配置；抛错即装载失败
    def teardown(self) -> None: ...   # 释放资源；卸载与回滚都会调用
```

### `GET /v1/plugins`

```json
200 [{"plugin_id": "sqlite", "version": "0.1.0", "description": "...",
      "tools": [{"name": "search_database", "contract_version": "1.0"}]}]
```

### `POST /v1/tools/execute` → `search_database`

请求：

```json
{"tool": "search_database",
 "arguments": {"table": "drivers", "columns": ["id", "name"],
               "filters": {"status": "active"}, "limit": 2, "offset": 0}}
```

成功（200）：

```json
{"tool": "search_database", "contract_version": "1.0",
 "result": {"table": "drivers", "columns": ["id", "name"],
            "rows": [{"id": 1, "name": "张三"}, {"id": 2, "name": "李四"}],
            "row_count": 2, "total": 5, "truncated": true}}
```

语义要点：`rows` 里的键一定属于 `columns`；`total` 是**同一过滤条件下的命中总数**（用于翻页，不计 limit/offset）；`truncated = row_count < total`；BLOB 以 base64 字符串返回（不静默丢数据）。

失败：沿用阶段 1 的信封，新增两类 `details` 标签，仍走 422 `invalid_arguments`：

| `details[].type` | 含义 |
|---|---|
| `unknown_table` | 表不在库里，`msg` 里给出可用表名 |
| `unknown_column` | 列不在表里，`msg` 里给出该表可用列名 |

启动期配置错误（进程起不来，不是请求错误）：`unknown_plugin`、`invalid_configuration`、`plugin_load_failed`，均为 500 且信息含具体插件 id / 路径。

## 关键代码

```python
def load(self, plugin: Plugin) -> None:
    plugin.setup()                       # 失败直接冒泡，注册表未被污染
    registered: list[str] = []
    try:
        for tool in plugin.tools():
            self._registry.register(tool)
            registered.append(tool.name)
    except PlatformError:
        for name in reversed(registered):  # 原子性：不留半个插件
            self._registry.unregister(name)
        plugin.teardown()
        raise
    self._plugins[plugin.plugin_id] = _LoadedPlugin(plugin, registered)
```

```python
def _quote_identifier(name: str) -> str:        # 标识符只可能来自已校验的 schema
    if not IDENTIFIER_PATTERN.match(name):
        raise ...
    return '"' + name.replace('"', '""') + '"'

sql = f"SELECT {cols} FROM {_quote_identifier(table)}"
if filters:
    sql += " WHERE " + " AND ".join(f"{_quote_identifier(k)} = ?" for k in filters)
sql += " LIMIT ? OFFSET ?"                      # 值永远是占位符，不拼进 SQL
```

## 测试与部署方案

- 插件宿主：装载后工具可见、卸载后工具消失、`teardown` 被调用、工具名冲突时回滚且注册表干净、卸载未知插件报 `plugin_not_found`。
- `search_database`：全表/投影/等值过滤/分页与 `total`/`truncated`/空结果/未知表/未知列/BLOB 转 base64/`limit` 越界。
- 配置：默认不装任何插件、`DRIVER_PLUGINS` 逗号分隔与空白处理、启用 sqlite 缺路径或文件不存在时启动失败、`DRIVER_PLUGINS` 含未知 id 时启动失败。
- 手工冒烟：先用 python 造一个临时 SQLite 库，再带环境变量起服务：

```bash
DRIVER_PLUGINS=sqlite DRIVER_SQLITE_PATH=/tmp/demo.db \
  .venv/Scripts/python -m uvicorn driver_platform.main:app --port 8000
curl http://127.0.0.1:8000/v1/plugins
curl -X POST http://127.0.0.1:8000/v1/tools/execute -H "Content-Type: application/json" \
     -d '{"tool":"search_database","arguments":{"table":"drivers","limit":2}}'
```

CI 在阶段 1 的基础上增加两步：`ruff format --check src tests` 与 `mypy`（`[tool.mypy] files = ["src"]`，阶段 1 的契约已定型，静态检查不再是对着空接口调类型）。

## 非目标（留给后续阶段）

- 原始 SQL / join / 聚合查询：等阶段 4 的权限层，避免把库直接敞开给模型
- 通过 HTTP 动态加载插件、远程插件（阶段 6）
- 鉴权与权限（阶段 4）、request_id 与指标（阶段 5）、配置热更新
- 连接池与并发调优：SQLite 插件持单连接 + 锁，够本阶段用

## 已知限制

- SQLite 插件在单个连接上串行执行，慢查询会阻塞其他调用（阶段 5 会看到这一项指标）。
- `total` 需要额外一次 `COUNT(*)`，大表上有成本；分页深度上限没有限制。

## 验收证据

- `ruff check .` → `All checks passed!`
- `ruff format --check src tests` → `24 files already formatted`
- `mypy` → `Success: no issues found in 17 source files`
- `pytest` → `56 passed`（阶段 1 的 17 条契约测试保持不变，新增插件宿主 / 配置 / `search_database` 用例）
- 带 `DRIVER_PLUGINS=sqlite` + 临时库起 uvicorn：`GET /v1/plugins` 列出 sqlite 插件、`GET /v1/tools` 含 `search_database`、`POST /v1/tools/execute` 返回结构化行、未知表 → 422 `unknown_table`

