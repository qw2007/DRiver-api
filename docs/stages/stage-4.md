# 阶段 4：安全权限（凭证 + 工具作用域）

> 目标：无凭证拒绝、越权被拦。状态：已完成。

## 选型理由

**为什么是「API Key + 每把钥匙的工具作用域」，而不是 OAuth / JWT / 用户体系**：这个平台的调用方是 Agent 与运维脚本，不是人——没有登录页、没有浏览器回调、没有租户注册、没有刷新令牌的轮换需求。OAuth 会一次性引入授权服务器、回调地址、令牌轮换三套运维负担，而阶段 5（可观测性）与阶段 6（远程插件）都还没到，现在做等于给一个没人访问的服务配门禁系统。API Key 是**最小可用**的凭据：一行环境变量、一次常数时间比较、一条可审计的指纹。将来真要接企业身份，凭据解析（`security/http.py`）是唯一的改造点，作用域模型（`Principal`）不用动。

**为什么权限落在「工具」这一层**：中间层唯一能做的事就是调工具——它不存业务数据、不渲染界面、不跑定时任务。所以权限模型只有一层：**这把钥匙能用哪些工具**。再往上加「能不能读某个表」「能不能看某一列」，那是插件自己的业务（`search_database` 的表/列校验已经在阶段 2 做完了），中间层不该替它发明第二套规则。

**为什么作用域是允许列表（allowlist）而不是拒绝列表**：默认全开 + 黑名单，漏写一条就是权限泄漏，而且泄漏是静默的；白名单漏写一条只是少了个功能，症状立刻可见（403）。安全默认值必须取严：`secret` 不带作用域 = 全部工具（显式写法是 `secret:*`），带作用域就严格按列表放行。

**为什么默认 `off`，而不是一上来就强制凭证**：阶段 0–3 的本地开发、全部测试、以及 stdio MCP 都是「进程由 Agent 自己 spawn」——没有监听端口，也就没有「谁都能连」的攻击面。默认强制凭证会把「起服务」变成必须先配密钥，直接把前四个阶段的用法全部打断。风险用两件事兜住：**(a)** 鉴权关闭时启动日志明确警告（不是静默）；**(b)** 开放模式下**带了凭证就必须有效**——写错的密钥回 401，不会被当成匿名放行。所谓「默认关」只关掉了「强制出示」，没有关掉「出示了就要对」。

**为什么 MCP 一侧用 `DRIVER_API_KEY`（单数），服务端用 `DRIVER_API_KEYS`（复数）**：服务端持有的是**钥匙清单**；stdio 子进程只应该知道**自己那一把**。MCP 客户端的子进程环境来自 Agent 的配置，把整份清单塞进去等于顺手把别人的钥匙也发给了它——单复数两套名字让这件事在配置里一眼可见。

**为什么鉴权不写成一个全局中间件**：中间件对所有路径生效，会把 `/health` 也挡掉——探针拿不到 200 就该重启服务了，这是拿可用性换来的假安全；而且中间件运行时还不知道「这次要调哪个工具」，作用域判断必须发生在**知道工具名之后**。所以鉴权分成两处：FastAPI 依赖负责「凭证 → Principal」（401），执行管线负责「Principal 能不能用这个工具」（403）。

**为什么按凭证过滤工具列表**：Agent 看到调不了的工具，会试着调用、吃一个 403、再浪费一轮上下文自我纠正。把不可见的工具从 `tools/list`、`GET /v1/tools`、`GET /v1/plugins` 里摘掉，等于**能力不可见即不可用**，同时少一次信息泄漏。可见性与权限用**同一个** `Principal.allows()` 判定，不存在「列表里藏着、调用却放行」的错位。

**为什么越权回 403 而不是假装 404**：工具名不是秘密（`/v1/tools` 本来就会列出来），而 404 会让运维去翻注册表、怀疑插件没装。403 明确说「凭证有效，但作用域不够」，并在 `details` 里回**该凭证自己**的允许列表——这是它自己的权限，不是别人的秘密。

## 模块设计

```
src/driver_platform/
  config.py                 + DRIVER_AUTH_MODE / DRIVER_API_KEYS（跨字段校验在 __post_init__）
  security/keys.py          ApiKey / Principal / parse_api_keys()（纯数据与解析，零依赖）
  security/policy.py        authenticate()（凭证 → Principal）+ visible_manifests()
  security/http.py          extract_secret()（两个头）+ principal_of()（FastAPI 依赖）
  tools/executor.py         execute_tool(..., principal=)：取工具 → **鉴权** → 校验 → 执行
  api/tools.py              列表按作用域过滤；执行传 principal
  api/plugins.py            插件列表按作用域过滤（工具全不可见的插件不出现）
  protocols/mcp.py          build_server(registry, principal)：tools/list 过滤、tools/call 鉴权
  protocols/mcp_server.py   从 DRIVER_API_KEY 解析凭证（在装插件之前）
```

三个入口（HTTP / MCP / 未来阶段 6 的远程插件）共用 `security.policy` 里**同一段**判定逻辑，适配器只负责「凭证从哪来」：HTTP 从请求头，stdio 从环境变量。`security/` 不认识 FastAPI 以外的协议，也不认识任何具体工具。

## 接口定义

### 环境变量

| 变量 | 默认 | 取值 | 语义 |
| --- | --- | --- | --- |
| `DRIVER_AUTH_MODE` | `off` | `off` / `required` | `required` 时无凭证一律 401；写错值启动即失败 |
| `DRIVER_API_KEYS` | 空 | `secret[, secret:tool1\|tool2][, secret:*]` | 服务端持有的钥匙清单与各自作用域 |
| `DRIVER_API_KEY` | 空 | 单个 secret | **仅 MCP stdio 客户端进程**用：自己那一把钥匙 |

`secret` 里不允许出现 `:`、`|`、`,`（否则解析歧义），违反即 `invalid_configuration`。`required` 但清单为空 → 同样启动即失败。

### 凭证传递（HTTP）

```
Authorization: Bearer <secret>      # 优先
X-API-Key: <secret>                 # 兼容不支持自定义 Authorization 的客户端
```

两个头都没带且 `required` → `missing_credentials`；带了但不在清单里 → `invalid_credentials`；
`Authorization` 用了非 `Bearer` 方案（如 `Basic`）→ `invalid_credentials`（明确提示改用 Bearer）。

### 新增错误码

| 情况 | code | HTTP | MCP（stdio） |
| --- | --- | --- | --- |
| 该给凭证却没给 | `missing_credentials` | 401 | 进程启动失败，退出码 2 |
| 凭证不在清单里 | `invalid_credentials` | 401 | 进程启动失败，退出码 2 |
| 凭证有效、工具不在作用域内 | `permission_denied` | 403 | `CallToolResult(isError=true)`，同信封 |

`/health` 永远公开（探针不该带凭证）。`code` 属于对外契约，新增算兼容变更。

### 用法示例

```bash
# 服务端：两把钥匙，一把全权，一把只能用 echo
DRIVER_AUTH_MODE=required DRIVER_API_KEYS="s3cret-full, s3cret-echo:echo" \
  uvicorn driver_platform.main:app

curl.exe -s -H "Authorization: Bearer s3cret-echo" http://127.0.0.1:8000/v1/tools
# → [echo]

curl.exe -s -X POST -H "Authorization: Bearer s3cret-echo" -H "Content-Type: application/json" \
  --data-binary '{"tool":"search_database","arguments":{}}' \
  http://127.0.0.1:8000/v1/tools/execute
# → 403 {"error":{"code":"permission_denied", ...}}
```

## 关键代码

```python
# security/policy.py —— 凭证 → 主体；HTTP 与 MCP 共用
def authenticate(settings: Settings, secret: str | None) -> Principal:
    if not secret:
        if settings.auth_mode == AUTH_MODE_REQUIRED:
            raise MissingCredentialsError("缺少凭证……")
        return ANONYMOUS                     # 开放模式：匿名全权
    for key in settings.api_keys:
        if hmac.compare_digest(key.secret, secret):   # 常数时间比较
            return principal_for(key)
    raise InvalidCredentialsError("凭证无效")          # 带了就必须有效

# tools/executor.py —— 鉴权夹在「取工具」与「校验入参」之间
tool = registry.get(name)                     # 404 tool_not_found
if not principal.allows(tool.name):
    raise PermissionDeniedError(...)          # 403 permission_denied
validated = tool.input_model.model_validate(arguments)   # 422 invalid_arguments
```

顺序是刻意的：**先认工具、再判权限、最后校验入参**。所以「无权」永远不会被伪装成「参数错误」，Agent 拿到 403 就知道该换钥匙，而不是去猜自己的 JSON 哪里写错了。

## 测试与部署方案

- `tests/test_security.py`：配置校验（未知模式、`required` 无清单、五种非法密钥写法）、无凭证/坏凭证/非 Bearer 方案、两种头都可用、开放模式下坏凭证仍 401、`/health` 公开、作用域过滤（工具列表、插件列表）、越权 403、有权 200、无权时未知工具仍是 404。
- `tests/test_mcp_scopes.py`：真子进程 + 官方客户端——作用域钥匙 `tools/list` 只见自己的工具、越权调用 `isError=true` + `permission_denied`；全权钥匙两者都在；`required` 但没给 `DRIVER_API_KEY` 时进程退出码 2 且 stderr 上是 `missing_credentials`。
- 部署：`DRIVER_API_KEYS` 只从环境变量读（原则「配置外部化」），阶段 7 的 Compose 会把密钥接成 secret；CI 不需要改（密钥在测试里显式传 `Settings`，不读进程环境）。

## 非目标（本阶段明确不做）

- OAuth2 / JWT / 用户与角色体系、登录流程、密钥轮换与吊销列表；
- 速率限制、配额、审计日志（属阶段 5 可观测性）；
- TLS/反代、网络边界（属阶段 7 部署）；
- 参数级授权（「同一个工具只能查某几张表」）——那是插件自己的业务规则。

## 已知限制

- 默认 `off`：不配 `DRIVER_AUTH_MODE=required` 就是开放的，只能在受信网络（本机 / 内网）跑；启动日志会警告。
- 密钥明文存在于环境变量（进程内可读）；日志里只出现指纹（`sha256(secret)[:8]`），但环境变量本身需要由部署方保护。
- 作用域只到工具名，不支持通配（`search_*`）与参数级约束。
- 鉴权只解决「谁能调」，不解决「调多少次」「调了什么都留痕」——两者都在阶段 5。
- 鉴权依赖先于协议开关判定：`required` 模式下，即使某协议没启用，未出示凭证的调用方也只会看到 401——这是刻意的（先证明你是谁，再谈有没有这个功能）。

## 验收证据

门禁（本机实测）：

```
python -m ruff check .                     → All checks passed!
python -m ruff format --check src tests    → 40 files already formatted
python -m mypy src                         → Success: no issues found in 27 source files
python -m pytest -p no:cacheprovider       → 98 passed
```

98 条里本阶段新增 24 条（`tests/test_security.py` 21 + `tests/test_mcp_scopes.py` 3）；阶段 1–3 的用例一条没改，全部仍然通过（默认 `off` 对既有行为零影响）。

真实服务冒烟（`uvicorn 127.0.0.1:8140`，`DRIVER_AUTH_MODE=required`、`DRIVER_API_KEYS="key-full-0001, key-echo-0002:echo"`、`DRIVER_PLUGINS=sqlite`、`DRIVER_PROTOCOLS=openai`、3 行 `drivers` 表）：

| 请求 | 结果 |
| --- | --- |
| `GET /health`（无凭证） | 200 `{"status":"ok"}` |
| `GET /v1/tools`（无凭证） | 401 `missing_credentials` |
| `GET /v1/tools`（`Bearer nope`） | 401 `invalid_credentials` |
| `POST /v1/tools/execute`（无凭证） | 401 `missing_credentials` |
| `GET /v1/tools`（`X-API-Key: key-echo-0002`） | 200，只剩 `echo` |
| `GET /v1/plugins`（作用域钥匙） | 200 `[]`（sqlite 的唯一工具不可见） |
| `POST execute echo`（作用域钥匙） | 200 `{"echo":"hi","times":1}` |
| `POST execute search_database`（作用域钥匙） | 403 `permission_denied`，`details[0].msg` = 「该凭证可用的工具：echo」 |
| `POST execute search_database`（全权钥匙） | 200 结构化结果 |
| `GET /v1/protocols/openai/tools`（作用域钥匙） | `tools` 只有 `echo`；全权钥匙为 `echo` + `search_database` |

MCP 一侧（`tests/test_mcp_scopes.py`：真子进程 + 官方客户端）：

- 作用域钥匙：`tools/list` 只返回 `echo`；调用 `search_database` 得 `isError=true` 且 `structuredContent.error.code == "permission_denied"`。
- 全权钥匙：两个工具都在，`search_database` 正常返回结构化结果。
- `required` 但进程环境里没有 `DRIVER_API_KEY`：退出码 2，stderr 上是 `missing_credentials` 信封——**不会**降级成匿名服务。

运行环境备注：本机受限沙箱跑不了使用 `tmp_path` 的测试（pytest 建目录用 `mode=0o700`，受限令牌无权访问），全量 pytest 用一次性提权（`TEMP` 指向系统临时目录）执行；CI（ubuntu-latest、Python 3.14）不受影响。
