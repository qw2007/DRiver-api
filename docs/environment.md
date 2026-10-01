# 环境与工具手册

本文件记录**本机 Windows 开发环境**的实测事实与本项目的落地解法：装了什么、哪些命令要提权、环境坑怎么绕过。
CI（GitHub Actions `ubuntu-latest` + Python 3.14）不受本文件约束——CI 里所有命令都不需要提权。

实测时间：2026-10-01，工作目录 `D:\DSh\driver-platform-v2`。

## 1. 运行时与工具清单

| 工具 | 版本 / 位置 | 用途 | 备注 |
| --- | --- | --- | --- |
| Python | 3.14.7（`C:\Python314\python.exe`） | 全部服务端代码 | 本机唯一 3.14 系列；`.venv/pyvenv.cfg` 记 `home = C:\Python314` |
| venv | `.venv`（`python -m venv --without-pip` 创建） | 依赖隔离 | 用 `--without-pip` 绕开沙箱 TEMP 不可写，再单独 ensurepip |
| pip | 26.2.1 | 依赖安装 | 安装动作需提权（见 §4.2） |
| Node.js | v24.19.0（`npm` 11.17.0、`npm.cmd`、`pnpm`） | 仅本机工具链，与项目运行无关 | |
| Git | 2.45.1.windows.1 | 版本控制 | 已配置 schannel 直连，见 §5 |
| Docker | 29.8.1（Docker Desktop，linux 引擎，`overlayfs`，16 CPU / 16 GB） | 阶段 5 的 Prometheus/Grafana、阶段 7 的 compose | 数据盘已迁到 `D:\Develop\Docker\DockerDesktopWSL`，见 §6 |
| docker compose | v5.5.1 | 阶段 7 `docker compose up` | 阶段 7 才会验证 |
| kubectl | v1.36.1 | 阶段 8（多副本/滚动更新）预备 | 未接集群，阶段 8 才用 |
| GitHub CLI `gh` | 2.102.0（`D:\DSh\tools\bin\gh.exe`） | 查仓库、建仓库、查 Actions | 非 choco 安装，见 §4.5 |
| jq | 1.8.2（`D:\DSh\tools\bin\jq.exe`） | 解析 JSON 响应 | 同上 |
| Chocolatey | 2.7.4 | 唯一存在的系统级包管理器 | 非提权无法写 `lib-bad`，见 §4.5 |

`.venv` 内关键 Python 包（`python -m pip list` 实测）：

| 包 | 版本 | 归属 |
| --- | --- | --- |
| fastapi | 0.142.1 | 运行时依赖 |
| uvicorn | 0.54.0 | 运行时依赖（`[standard]`） |
| pydantic | 2.13.5 | 运行时依赖 |
| prometheus-client | 0.26.0 | 运行时依赖（阶段 5 的 `/metrics`） |
| mcp | 1.30.0 | 可选依赖 `[mcp]`（阶段 3） |
| httpx | 0.28.1 | dev（测试客户端） |
| pytest | 9.1.1 | dev |
| pytest-cov | 7.1.0（coverage 7.16.2） | dev |
| ruff | 0.16.9 | dev（格式 + 静态检查） |
| mypy | 2.3.1 | dev（严格模式） |
| bandit | 1.9.4 | dev（安全检查） |
| pip-audit | 2.10.1 | dev（依赖漏洞审计） |

**未安装且当前不需要**（已评估，避免"先装了再说"）：`uv` / `poetry` / `pipx` / `hatch` / `twine` / `pre-commit`（项目用 `.venv` + ruff/mypy 直接跑）；`make` / `gcc` / `cl.exe`（无编译步骤）；`psql` / `sqlite3` CLI / `redis-cli` / `mongosh`（数据库插件走 Python 驱动与内存库）；`nginx` / `caddy` / `nc`（阶段 7 用容器）；`helm` / `kind` / `minikube`（阶段 8 再说）；独立 OpenSSL（Git 自带即可）。

## 2. 阶段 5 相关资源（本次补齐）

- **Python 侧**：`prometheus-client==0.26.0` 已进 `dependencies`（`/metrics` 端点用；特性开关默认关）。
- **容器镜像**（已拉取，注意当前是 `latest`）：

  | 镜像 | 摘要 | 大小 |
  | --- | --- | --- |
  | `prom/prometheus:latest` | `sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e` | 381 MB |
  | `grafana/grafana:latest` | `sha256:b28bae15e219c998fb0e0424ed724930cc61b1f61fb404d47c862f9a23f9e572` | 1.94 GB |

  **阶段 5 落地时要收尾的事**：把这两个 `latest` 换成上表的摘要（`prom/prometheus@sha256:...`），否则"可复现部署"不成立。
- **度量口径**：`/metrics` 暴露 QPS / 延迟 / 错误率，日志按 `request_id` 串联全链路（阶段 5 设计说明里落定标签集合）。

## 3. 常用命令

门禁（提交前跑）：

```powershell
.\.venv\Scripts\python.exe -m ruff check .                      # 受限模式即可
.\.venv\Scripts\python.exe -m ruff format --check src tests      # 受限模式即可（见下方注）
.\.venv\Scripts\python.exe -m mypy src                           # 受限模式即可
.\.venv\Scripts\python.exe -m bandit -r src -q                   # 受限模式即可
.\.venv\Scripts\python.exe -m pip_audit --cache-dir .pip-audit-cache --progress-spinner off
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q      # ⚠ 全量必须提权，见 §4.1
```

注：`ruff format --check .` 在仓库根会崩（`Expected a ruff source file`），根因是仓库根的临时残留目录；用 `src tests` 显式限定即可。

测试分档（实测）：

| 跑法 | 结果 |
| --- | --- |
| 提权全量 `pytest` | **98 passed**（exit 0） |
| 受限全量 `pytest` | 68 passed / **30 errors**，全部是 `tmp_path` 类夹具的 `PermissionError`（`test_search_database` 18、`test_security` 6、`test_mcp_scopes` 2、`test_mcp_server` 2、`test_config` 1、`test_openai_schema` 1） |
| 受限跑不含 `tmp_path` 的文件 | 正常通过 |

安全审计：

- `pip-audit`：`No known vulnerabilities found`（**必须带 `--cache-dir .pip-audit-cache`**，受限模式建不了默认缓存目录 `%LOCALAPPDATA%\pip-audit`）。
- `bandit`：exit 0。`src/driver_platform/plugins/sqlite.py` 的 2 条 `B608` 属误报，已用 `# nosec B608` 就地标注并写明理由（SQL 结构由白名单校验 + 参数绑定拼装），bandit 会附带一条 `nosec encountered (B608), but no failed test ...` 的良性警告。

## 4. 沙箱约束与解法（原「五」）

### 4.1 机制：受限令牌 + `0o700` 目录 = 谁都进不去

DSH 在 `workspace-write` 模式下用**受限令牌**跑命令，该令牌里 `BUILTIN\Administrators` 是 **deny-only**（UAC 过滤令牌的固有行为），且不能打开命名管道。于是：

- 普通方式建的目录（继承默认 ACE）→ 枚举/写入/删除都正常；
- 用 `os.mkdir(..., 0o700)` / `tempfile.mkdtemp()` 建的目录（**Python 默认权限**）拿到的是**不继承的受保护 DACL**：

  ```
  NT AUTHORITY\SYSTEM:(OI)(CI)(F)
  BUILTIN\Administrators:(OI)(CI)(F)
  OWNER RIGHTS:(OI)(CI)(F)
  Mandatory Label\Low Mandatory Level:(I)(OI)(CI)(NW)
  ```

  受限令牌既拿不到 `Administrators` 的授权（deny-only），也拿不到 `OWNER RIGHTS` 的授权 → **连创建它的进程自己都读不了、删不掉**。

实测（`.probe` 三连）：

| 目录 | 创建方式 | 结果 |
| --- | --- | --- |
| `.probe\plain` | 默认权限 | 可枚举 / 可写 / 可删 |
| `.probe\m777` | `mode=0o777` | 可枚举 / 可写 / 可删 |
| `.probe\m700` | `mode=0o700`（= pytest 的做法） | **拒绝**：`Access to the path '...m700' is denied` |

这就是 **pytest `tmp_path` 在受限模式下必然失败** 的根因：pytest 用 `Path.mkdir(mode=0o700)` 建 `pytest-of-<user>`，随后自己都进不去（`ERROR ... PermissionError: [WinError 5]`）。
`--basetemp=<工作区内已存在目录>` **不能绕过**：pytest 会先删掉再按同样方式重建（`_pytest/tmpdir.py:315-340`，`pytest_sessionfinish` 里 `cleanup_dead_symlinks(basetemp)` 直接 `PermissionError`）。

### 4.2 需要提权的操作（清单）

| 操作 | 原因 |
| --- | --- |
| `pip install` / `pip uninstall` / `ensurepip` | 要写工作区外的 site-packages 与沙箱不可写的 TEMP |
| 全量 `pytest` | `tmp_path` 建 `0o700` 目录（§4.1） |
| 所有 `docker` 命令 | CLI 要连命名管道 `//./pipe/docker_engine`，受限进程不能开管道 |
| Git 自带的 msys 程序（如 `openssl.exe`） | 受限模式下直接 `0xC0000005`（访问冲突） |
| 写工作区外的文件（`~/.gitconfig`、用户 `PATH`、`D:\DSh\tools`） | 沙箱只授权工作区 |

### 4.3 标准提权调用

对该条命令用一次性更宽权限重试（会先征求用户批准）：

```
sandbox_permissions: danger-full-access
```

例：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"      # 需 danger-full-access
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q # 需 danger-full-access
docker images                                               # 需 danger-full-access
```

提权进程留下的 `0o700` 目录（`pytest-of-Administrator`、`pytest-cache-files-*`）受限模式**连读都拒绝**，必须再提权删：

```powershell
icacls pytest-of-Administrator /reset /t /c /q
Remove-Item -Recurse -Force pytest-of-Administrator
```

**残留会落在工作区根部**：本机 `tempfile.gettempdir()` 取不到可用的 `TEMP`，退化成当前目录，于是 pytest 的 basetemp 变成 `D:\DSh\driver-platform-v2\pytest-of-Administrator\`（提权跑也一样）。`.gitignore` 已收录 `pytest-of-*/` 与 `pytest-cache-files-*/`，不会误提交；但受限模式删不掉它，清理必须提权。

### 4.4 根治办法（需要人工操作）

把本机 DSH 的**权限模式切成 `danger-full-access`**，沙箱不再介入，上述限制（提权、TEMP、管道、`0o700`）全部消失。代价是失去沙箱对文件写入的保护——是否切换是人的决定，本文只记录事实与影响面。CI 不受影响。

### 4.5 包管理器事实

- `choco install gh jq -y` **失败**：`Cannot create directory "C:\ProgramData\chocolatey\lib-bad". Error was: System.UnauthorizedAccessException`。即使给足文件访问权限，非提权（UAC 过滤令牌）进程仍写不了 `C:\ProgramData\chocolatey`。
- 代价最低的替代：从 GitHub Releases 下载 zip 到 `D:\DSh\tools\bin`（`curl.exe -sSL --ssl-no-revoke`），再把该目录追加进**用户** `PATH`——不需要管理员。
- 本机 `winget` 在受限模式下无输出（不可用）；无 scoop。

## 5. GitHub 直连（已修复）

| 阶段 | 症状 |
| --- | --- |
| 受限模式 `curl` | `schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS (0x8009030e)` |
| 受限模式 `git`（openssl 后端，加 `schannel` 前） | `SSL certificate problem: unable to get local issuer certificate` |
| 受限模式 `git`（schannel 后端，加 `schannel` 后） | 同样 `SEC_E_NO_CREDENTIALS (0x8009030E)` —— 受限令牌拿不到 schannel 的凭据句柄 |
| 提权 `curl https://github.com` | `CRYPT_E_NO_REVOCATION_CHECK (0x80092012)`（证书链可信，但吊销检查无法完成） |

**分工结论：联网的 Git 操作（`ls-remote` / `fetch` / `push`）一律提权**；提权后不需要再传任何 `-c` 参数。受限模式下的 git 只能做本地操作（`status` / `log` / `commit`）。

结论：**TLS 本身是通的，卡在"吊销检查"和"CA 来源"**；`ghproxy.net` 镜像只能读（fetch 得到），push 会被 GitHub 拒（`remote: Invalid username or token. Password authentication is not supported for Git operations.`）。

**已落盘的全局配置**（`C:\Users\Administrator\.gitconfig`）：

```
http.sslBackend = schannel
http.schannelCheckRevoke = false
```

同时**移除了**原有的 `url."https://ghproxy.net/https://github.com/".insteadOf = https://github.com/`（它会把 push 也改写到镜像）。备份：`C:\Users\Administrator\.gitconfig.bak-20261001-120108`（回滚＝覆盖回去）。

验证（提权；**不需要再传任何 `-c` 参数**）：

```powershell
git ls-remote origin          # -> 783b5c6... refs/heads/master   (exit 0)
git push --dry-run origin master   # -> Everything up-to-date     (exit 0)
```

凭据由 **Git Credential Manager** 管理（`credential.helper = manager`，来自系统配置 `C:\Program Files\Git\etc\gitconfig`）；首次推送会打开浏览器登录一次，之后凭据存入 Windows 凭据管理器。

## 6. Docker 镜像加速（已修复）

| 阶段 | 症状 |
| --- | --- |
| 引擎刚起来时 `docker pull` | `failed to resolve reference "docker.io/...": ... registry-1.docker.io ... connection reset by peer` |

解法：`C:\Users\Administrator\.docker\daemon.json` 增加 `registry-mirrors`（daocloud / 1panel / xuanyuan / 1ms），先备份 `daemon.json.bak`，再 `docker desktop restart`。

验证：`docker run --rm hello-world` → exit 0；`docker pull prom/prometheus`、`docker pull grafana/grafana` 均成功（摘要见 §2）。

数据盘位置（已迁移）：`D:\Develop\Docker\DockerDesktopWSL\disk\docker_data.vhdx`（数据，1.58 GB）+ `...\main\ext4.vhdx`（0.09 GB）；迁移后引擎仍正常（hello-world 退出码 0）。

## 7. 机器其他事实

- 磁盘：`C:` 约 33.9 GB 可用、`D:` 约 2.65 TB、`E:` 约 502 GB 可用 → **本机项目放 `D:\DSh`**。
- 本机**没有** `gh` 之外的 GitHub 工具；无 SSH 密钥（`~/.ssh/id_ed25519`、`~/.ssh/id_rsa` 均不存在）、无 `~/.git-credentials`（凭据在凭据管理器里）。
- Git 配置碎片：`~/.gitconfig` 里写坏的 `[usere] name = qw.pc` 是无效键（**`user.name` 实际未设置**，提交需显式 `-c user.name=...`，本项目提交用 `-c user.name="Wang Qian" -c user.email="Wang.qian__@outlook.com"`）；系统配置里 `init.defaultBranch = master`、`core.autocrlf = true`、`http.sslCAInfo = C:/Program Files/Git/mingw64/etc/ssl/certs/ca-bundle.crt`。

## 8. 一分钟自检

```powershell
.\.venv\Scripts\python.exe -V                        # Python 3.14.7
.\.venv\Scripts\python.exe -m ruff check .           # All checks passed!
.\.venv\Scripts\python.exe -m mypy src               # Success: no issues found in 27 source files
git ls-remote origin                                  # 需 §5 的 sslBackend=schannel
docker version --format '{{.Server.Version}}'         # 29.8.1（受限模式需提权）
```

任一项与上述不符，先按对应小节排查，再动业务代码。
