"""阶段 7：部署描述的契约测试。

这里**不起容器**（那是 CI 的 compose 冒烟任务与 `docs/stages/stage-7.md` 里的本机验收干的事），
只守住三件会在改动中悄悄坏掉、又不容易在本地发现的事：

1. compose 里写的环境变量名与平台真的会读的名字一致——打错一个字母，容器起来就是 500/启动失败；
2. 两个第三方镜像钉了摘要、本服务是 build 出来的——「可复现部署」不能靠 `latest`；
3. Grafana 的**面板 JSON ↔ 数据源 uid ↔ provisioning 路径**三处必须对得上，
   否则面板会静默变成 "Datasource prom-main was not found"。

另外还验证演示库种子与 README 的验收示例是同一份 schema（示例能跑通，不只是看起来对）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

import driver_platform as package_module
import driver_platform.config as config_module
import driver_platform.security.keys as keys_module
from driver_platform.security.keys import parse_api_keys

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
DOCKERFILE_PATH = ROOT / "Dockerfile"
DOCKERIGNORE_PATH = ROOT / ".dockerignore"
DEPLOY_PROMETHEUS = ROOT / "deploy" / "prometheus" / "prometheus.yml"
DASHBOARD_PATH = ROOT / "docs" / "observability" / "grafana-dashboard.json"
DATASOURCE_PATH = ROOT / "deploy" / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
DASHBOARD_PROVIDER_PATH = (
    ROOT / "deploy" / "grafana" / "provisioning" / "dashboards" / "driver-platform.yml"
)
ENTRYPOINT_PATH = ROOT / "deploy" / "docker" / "entrypoint.py"
SEED_PATH = ROOT / "deploy" / "demo" / "seed_demo_db.py"
README_PATH = ROOT / "README.md"

DIGEST_PATTERN = re.compile(r"^[\w./-]+@sha256:[0-9a-f]{64}$")
DASHBOARD_CONTAINER_PATH = "/var/lib/grafana/dashboards/driver-platform-overview.json"
#: 入口脚本自己的开关，不属于平台配置（平台不读它，只有容器入口读）。
CONTAINER_ONLY_ENV = {"DRIVER_SEED_DEMO_DB"}


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compose() -> dict[str, Any]:
    loaded = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _service(name: str) -> dict[str, Any]:
    service = _compose()["services"][name]
    assert isinstance(service, dict)
    return service


def _known_env_names() -> set[str]:
    names: set[str] = set()
    for module in (config_module, keys_module):
        for attribute, value in vars(module).items():
            if attribute.startswith("ENV_") and isinstance(value, str):
                names.add(value)
    return names


def test_every_compose_env_name_is_read_by_the_platform() -> None:
    """compose 里出现的 DRIVER_* 必须是平台真的会读的名字（或入口脚本自己的开关）。"""
    environment = _service("driver-platform")["environment"]
    assert isinstance(environment, dict)
    unknown = {
        key
        for key in environment
        if key.startswith("DRIVER_") and key not in _known_env_names() | CONTAINER_ONLY_ENV
    }
    assert unknown == set()


def test_platform_service_is_built_and_third_party_images_are_pinned() -> None:
    platform = _service("driver-platform")
    assert platform["build"] == {"context": "."}
    # 标签跟着包版本走：改版本号不该逼着改测试。
    assert platform["image"] == f"driver-platform:{package_module.__version__}"

    for name in ("prometheus", "grafana"):
        image = _service(name)["image"]
        assert DIGEST_PATTERN.match(image), f"{name} 必须钉摘要，实际：{image}"


def test_platform_service_runs_hardened() -> None:
    """阶段 8 加固：非 root、根文件系统只读、/tmp 内存盘、资源限额、不升权。"""
    platform = _service("driver-platform")
    assert platform["read_only"] is True
    assert "/tmp" in platform["tmpfs"]
    assert "no-new-privileges:true" in platform["security_opt"]
    assert platform["mem_limit"] == "512m"
    assert platform["cpus"] == "1.0"

    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "USER driver" in dockerfile
    assert "adduser -D -u 10001" in dockerfile
    assert "chmod -R a-w /app" in dockerfile


def test_demo_credentials_are_meant_to_be_overridden() -> None:
    """.env.example 必须存在且给出覆盖项；.env 本身要被 gitignore 掉。"""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DRIVER_API_KEYS=" in example
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert any(line.strip() == ".env" for line in gitignore.splitlines())


def test_platform_service_enables_observability_and_database_plugin() -> None:
    environment = _service("driver-platform")["environment"]
    assert environment[config_module.ENV_PLUGINS] == "sqlite"
    assert environment[config_module.ENV_SQLITE_PATH].startswith("/data/")
    assert environment[config_module.ENV_METRICS_ENABLED] == "1"
    assert environment[config_module.ENV_LOG_FORMAT] == "json"
    assert environment[config_module.ENV_AUTH_MODE] == "required"
    assert _service("driver-platform")["ports"] == ["${DRIVER_PORT:-8000}:8000"]


def test_platform_healthcheck_hits_the_health_endpoint() -> None:
    healthcheck = _service("driver-platform")["healthcheck"]
    assert "/health" in " ".join(healthcheck["test"])
    assert healthcheck["start_period"] == "5s"

    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile
    assert "/health" in dockerfile
    assert "ENTRYPOINT" in dockerfile
    assert 'CMD ["uvicorn"' in dockerfile
    assert "FROM python:3.14-alpine" in dockerfile
    assert ":latest" not in dockerfile


def test_dockerignore_keeps_the_context_small() -> None:
    ignored = {
        line.strip()
        for line in DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {".venv/", ".git/", "tests/", "docs/"} <= ignored
    # 镜像里必须有的东西不能被忽略掉。
    assert not {path for path in ignored if path.startswith("deploy")}


def test_compose_mounts_the_prometheus_and_grafana_configs() -> None:
    prometheus_volumes = _service("prometheus")["volumes"]
    assert "./deploy/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" in (
        prometheus_volumes
    )
    grafana_volumes = _service("grafana")["volumes"]
    assert "./deploy/grafana/provisioning:/etc/grafana/provisioning:ro" in grafana_volumes
    assert (
        f"./docs/observability/grafana-dashboard.json:{DASHBOARD_CONTAINER_PATH}:ro"
        in grafana_volumes
    )


def test_grafana_does_not_auto_update_its_bundled_plugins() -> None:
    """Grafana 13 默认在启动时把自带插件升级到最新版：先停掉旧进程，再从
    storage.googleapis.com 下载新版本。受限网络下下载失败，自带的那份（含 prometheus
    数据源）会被一起注销——本机实测表现为 /api/ds/query 返回 plugin.notRegistered、
    面板「Datasource not found」。这两个开关必须保持关闭。
    """
    environment = _service("grafana")["environment"]
    assert isinstance(environment, dict)
    assert environment["GF_PLUGINS_PREINSTALL_DISABLED"] == "true"
    assert environment["GF_PLUGINS_PREINSTALL_AUTO_UPDATE"] == "false"


def test_prometheus_scrape_configs_differ_only_in_the_discovery_source() -> None:
    """两份抓取配置只允许在「怎么找到目标」上不同。

    - compose 侧（阶段 8）：``dns_sd_configs`` 按服务名发现**全部**副本，副本数变了不用改配置；
    - 宿主侧（阶段 5 起）：平台用 uvicorn 直起，只有一个固定地址可抓。
    """
    compose_config = yaml.safe_load(DEPLOY_PROMETHEUS.read_text(encoding="utf-8"))
    host_config = yaml.safe_load(
        (ROOT / "docs" / "observability" / "prometheus.yml").read_text(encoding="utf-8")
    )

    assert compose_config["global"] == host_config["global"]
    compose_job = compose_config["scrape_configs"][0]
    host_job = host_config["scrape_configs"][0]
    assert compose_job["job_name"] == host_job["job_name"] == "driver-platform"
    assert compose_job["metrics_path"] == host_job["metrics_path"] == "/metrics"
    assert compose_job["dns_sd_configs"] == [
        {
            "names": ["driver-platform"],
            "type": "A",
            "port": 8000,
            "refresh_interval": "15s",
        }
    ]
    assert host_job["static_configs"][0]["targets"] == ["host.docker.internal:8000"]

    # 除了发现方式，两份配置的其余部分必须逐字相同。
    assert {key: value for key, value in compose_job.items() if key != "dns_sd_configs"} == {
        key: value for key, value in host_job.items() if key != "static_configs"
    }


def test_datasource_uid_matches_the_dashboard_panels() -> None:
    datasources = yaml.safe_load(DATASOURCE_PATH.read_text(encoding="utf-8"))["datasources"]
    assert len(datasources) == 1
    datasource = datasources[0]
    assert datasource["uid"] == "prom-main"
    assert datasource["url"] == "http://prometheus:9090"
    assert datasource["isDefault"] is True

    dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
    for panel in dashboard["panels"]:
        assert panel["datasource"] == {"type": "prometheus", "uid": datasource["uid"]}


def test_dashboard_provider_points_at_the_mounted_directory() -> None:
    provider = yaml.safe_load(DASHBOARD_PROVIDER_PATH.read_text(encoding="utf-8"))["providers"][0]
    mounted_directory = DASHBOARD_CONTAINER_PATH.rsplit("/", 1)[0]
    assert provider["options"]["path"] == mounted_directory
    assert provider["allowUiUpdates"] is False
    assert provider["disableDeletion"] is True


def test_dashboard_is_import_ready_and_provisioning_ready() -> None:
    raw = DASHBOARD_PATH.read_text(encoding="utf-8")
    # 阶段 5 的 __inputs/${DS_PROMETHEUS} 占位符会让 provisioning 直接失效（它不做替换），
    # 所以这里必须是「自带 uid」的形态：手工导入与自动装配共用同一份文件。
    assert "__inputs" not in raw
    assert "${" not in raw

    dashboard = json.loads(raw)
    assert dashboard["uid"] == "driver-platform-overview"
    assert dashboard["title"]
    assert dashboard["schemaVersion"] >= 39
    assert len(dashboard["panels"]) == 4
    for panel in dashboard["panels"]:
        assert panel["targets"][0]["expr"].strip()
        assert panel["description"].strip()


def test_dashboard_has_exactly_one_definition_in_the_repo() -> None:
    """面板只有一份真话：deploy/ 下不许再放一份（两份必然漂移）。"""
    duplicates = [
        path
        for path in (ROOT / "deploy").rglob("*")
        if path.is_file() and "panels" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert duplicates == []


def test_compose_api_keys_default_parses_into_two_scoped_keys() -> None:
    raw = _service("driver-platform")["environment"][keys_module.ENV_API_KEYS]
    assert isinstance(raw, str)
    match = re.fullmatch(r"\$\{[\w]+:-([^}]+)\}", raw)
    assert match is not None, f"演示钥匙应当写成 ${{VAR:-默认值}}，实际：{raw}"

    keys = parse_api_keys(match.group(1))
    assert {key.secret for key in keys} == {"admin-key", "read-key"}
    scopes = {key.secret: key.scopes for key in keys}
    assert scopes["admin-key"] is not None and "plugin_admin" in scopes["admin-key"]
    assert scopes["read-key"] is not None and "search_database" in scopes["read-key"]


def test_seed_demo_db_builds_the_table_the_readme_example_queries(tmp_path: Path) -> None:
    seed_module = _load_module("seed_demo_db", SEED_PATH)
    database = tmp_path / "nested" / "demo.db"

    assert seed_module.seed(database) is True
    connection = sqlite3.connect(database)
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(customers)")]
        rows = connection.execute("SELECT name, city FROM customers WHERE city = ?", ("上海",))
        matched = rows.fetchall()
    finally:
        connection.close()

    assert {"name", "city", "plan"} <= set(columns)
    assert matched
    # 幂等：文件已存在时不再写一遍（入口脚本可能因为数据卷挂载被多次调用）。
    assert seed_module.seed(database) is False

    readme = README_PATH.read_text(encoding="utf-8")
    assert '"table": "customers"' in readme
    assert '"city": "上海"' in readme


def test_seed_installs_the_database_atomically(tmp_path: Path, monkeypatch: Any) -> None:
    """先写同目录临时文件、再原子换名。

    多副本共用数据卷时，N 个容器会在同一瞬间都看到「库不存在」：直接写同一个 SQLite 文件
    会 `database is locked`，或者留下半成品库——平台以只读打开它，之后**谁都查不了**。
    """
    seed_module = _load_module("seed_demo_db_atomic", SEED_PATH)
    database = tmp_path / "demo.db"
    installs: list[tuple[Path, Path]] = []
    real_replace = seed_module.os.replace

    def spy(source: Any, target: Any) -> None:
        assert Path(source).parent == database.parent, "临时文件必须与目标同目录才能原子换名"
        assert Path(source).name != database.name
        assert Path(source).exists()
        installs.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(seed_module.os, "replace", spy)
    assert seed_module.seed(database) is True
    assert len(installs) == 1
    assert installs[0][1] == database
    assert [path.name for path in tmp_path.iterdir()] == ["demo.db"]

    # 换名失败（磁盘满、被 kill）时必须把临时文件收干净，否则下一次启动会踩到它。
    broken = tmp_path / "broken" / "demo.db"

    def fail(source: Any, target: Any) -> None:
        raise OSError("boom")

    monkeypatch.setattr(seed_module.os, "replace", fail)
    with pytest.raises(OSError, match="boom"):
        seed_module.seed(broken)
    assert [path.name for path in (tmp_path / "broken").iterdir()] == []


def test_entrypoint_seeds_only_when_asked_and_then_execs(monkeypatch: Any) -> None:
    entrypoint = _load_module("deploy_entrypoint", ENTRYPOINT_PATH)

    monkeypatch.delenv("DRIVER_SEED_DEMO_DB", raising=False)
    assert entrypoint._seed_enabled() is False
    monkeypatch.setenv("DRIVER_SEED_DEMO_DB", "ON")
    assert entrypoint._seed_enabled() is True

    assert entrypoint.main(["entrypoint.py"]) == 2  # 没给命令 → 用不着启动

    calls: list[list[str]] = []
    monkeypatch.setenv("DRIVER_SQLITE_PATH", "/data/demo.db")
    monkeypatch.setattr(entrypoint.subprocess, "run", lambda *args, **kwargs: calls.append(args[0]))
    monkeypatch.setattr(entrypoint.os.path, "exists", lambda path: False)
    monkeypatch.setattr(entrypoint.os, "execvp", lambda program, argv: calls.append(argv))
    assert entrypoint.main(["entrypoint.py", "uvicorn", "driver_platform.main:app"]) == 0
    assert calls[0] == [
        entrypoint.sys.executable,
        entrypoint.SEED_SCRIPT,
        "--path",
        "/data/demo.db",
    ]
    assert calls[1] == ["uvicorn", "driver_platform.main:app"]
