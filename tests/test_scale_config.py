"""阶段 8：多副本与滚动更新的部署描述契约。

阶段 7 的 ``tests/test_deploy_config.py`` 守的是「单副本能不能起、环境变量名对不对」；
这里守的是多副本形态下**容易在改动中悄悄坏掉**的东西：

1. overlay 必须真的把平台的宿主端口摘掉（``!override`` 而不是空列表——Compose 对 ``ports``
   是合并语义，写 ``ports: []`` 摘不掉），否则 ``--scale 3`` 直接端口冲突；
2. nginx 的副本解析必须是**变量式 proxy_pass + resolver**（静态 upstream 只在启动时解析一次），
   并且开着 ``proxy_next_upstream``——这才是「滚动更新不中断」的机制，不是装饰；
3. 镜像的 CMD 必须带 ``--timeout-graceful-shutdown``（在途请求的收尾窗口）；
4. Prometheus 必须按 DNS 抓服务名（静态配置只会抓到一个副本 IP）；
5. 两个验收脚本必须是合法 Python（语法门禁）且探针的计数真的能判成败。

真滚动是 ``deploy/rollout.py`` 在本机跑的验收，不在 CI（理由见 docs/stages/stage-8.md）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY_PATH = ROOT / "docker-compose.scale.yml"
BASE_COMPOSE_PATH = ROOT / "docker-compose.yml"
NGINX_PATH = ROOT / "deploy" / "nginx" / "conf.d" / "default.conf"
DOCKERFILE_PATH = ROOT / "Dockerfile"
DEPLOY_PROMETHEUS = ROOT / "deploy" / "prometheus" / "prometheus.yml"
ROLLOUT_PATH = ROOT / "deploy" / "rollout.py"
PROBE_PATH = ROOT / "deploy" / "traffic_probe.py"

DIGEST_PATTERN = re.compile(r"^[\w./-]+@sha256:[0-9a-f]{64}$")
NGINX_CONTAINER_PATH = "/etc/nginx/conf.d/default.conf"


class _ComposeLoader(yaml.SafeLoader):
    """Compose 的 ``!override`` / ``!reset`` 标签 PyYAML 不认识：按值原样构造即可。

    测试要读的是「作者写了什么」，所以不需要模拟 Compose 的合并语义——
    合并结果由 ``docker compose config`` 在本机验收里核对。
    """


def _construct_tagged(loader: yaml.Loader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _construct_tagged)


def _load_module(name: str, path: Path) -> Any:
    """按路径加载脚本。

    必须先放进 ``sys.modules``：脚本里有 ``@dataclass``，而 dataclasses 解析字符串注解时
    会去 ``sys.modules[cls.__module__]`` 找自己的命名空间——没登记就会拿到 ``None``
    并抛 ``AttributeError: 'NoneType' object has no attribute '__dict__'``。
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _overlay() -> dict[str, Any]:
    loaded = yaml.load(OVERLAY_PATH.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    assert isinstance(loaded, dict)
    return loaded


def test_overlay_removes_the_published_port_of_the_platform() -> None:
    """平台不再发布宿主端口：三个副本抢同一个宿主端口会直接起不来。"""
    base_ports = yaml.safe_load(BASE_COMPOSE_PATH.read_text(encoding="utf-8"))["services"][
        "driver-platform"
    ]["ports"]
    assert base_ports == ["${DRIVER_PORT:-8000}:8000"]

    overlay_text = OVERLAY_PATH.read_text(encoding="utf-8")
    assert "ports: !override []" in overlay_text
    assert _overlay()["services"]["driver-platform"]["ports"] == []


def test_overlay_gateway_publishes_the_only_entry_point() -> None:
    gateway = _overlay()["services"]["gateway"]
    assert DIGEST_PATTERN.match(gateway["image"]), f"gateway 必须钉摘要，实际：{gateway['image']}"
    assert gateway["ports"] == ["${DRIVER_PORT:-8000}:80"]
    assert gateway["depends_on"]["driver-platform"] == {"condition": "service_healthy"}
    assert f"./deploy/nginx/conf.d/default.conf:{NGINX_CONTAINER_PATH}:ro" in gateway["volumes"]
    assert "/gateway-health" in " ".join(gateway["healthcheck"]["test"])


def test_nginx_resolves_replicas_per_request_and_retries() -> None:
    """变量式 ``proxy_pass`` + Docker 内嵌 DNS 才会每次请求解析到当时的副本集合。"""
    config = NGINX_PATH.read_text(encoding="utf-8")
    assert "resolver 127.0.0.11" in config
    assert 'set $backend "driver-platform"' in config
    assert "proxy_pass http://$backend:8000" in config
    assert "proxy_next_upstream" in config
    # POST /v1/tools/execute 也要能重试（当前工具全是只读查询）。
    assert "non_idempotent" in config
    # 静态 upstream 会把副本地址在启动时定死，多副本形态下必然抓错（注释里提到它不算数）。
    directives = [line.strip() for line in config.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in directives if line.startswith("upstream ")]


def test_image_waits_for_in_flight_requests_on_shutdown() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert 'CMD ["uvicorn"' in dockerfile
    assert '"--timeout-graceful-shutdown", "20"' in dockerfile


def test_image_carries_a_build_stamp() -> None:
    """镜像要能被区分：同源重建命中缓存时镜像 ID 不变，滚动验收就看不出新旧版本。"""
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "ARG DRIVER_BUILD_STAMP=dev" in dockerfile
    assert "LABEL driver-platform.build.stamp=$DRIVER_BUILD_STAMP" in dockerfile


def test_prometheus_scrapes_every_replica_through_dns() -> None:
    job = yaml.safe_load(DEPLOY_PROMETHEUS.read_text(encoding="utf-8"))["scrape_configs"][0]
    assert job["job_name"] == "driver-platform"
    assert job["metrics_path"] == "/metrics"
    assert "static_configs" not in job

    discovery = job["dns_sd_configs"][0]
    assert discovery["names"] == ["driver-platform"]
    assert discovery["type"] == "A"
    assert discovery["port"] == 8000
    # 每副本必须留下各自的 instance 标签（默认就是目标地址），写死标签会让指标互相覆盖。
    assert set(discovery) == {"names", "type", "port", "refresh_interval"}


def test_acceptance_scripts_are_valid_python_and_use_the_overlay() -> None:
    for path in (ROLLOUT_PATH, PROBE_PATH):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    rollout = _load_module("deploy_rollout", ROLLOUT_PATH)
    probe = _load_module("deploy_traffic_probe", PROBE_PATH)

    assert "docker-compose.scale.yml" in rollout.COMPOSE_FILES
    assert rollout.SERVICE == "driver-platform"
    # 停机窗口只有一处真话（Dockerfile）；脚本给 docker stop 的期限必须**大于**它，
    # 否则 uvicorn 还没收完尾就被 SIGKILL，优雅停机形同虚设。
    assert rollout.STOP_TIMEOUT_SECONDS > 20
    assert probe.DEFAULT_EXPECT == 200
    assert probe.DEFAULT_TARGET.endswith("/health")


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, log_format: str, *args: Any) -> None:
        """静音访问日志。"""


def test_probe_counts_successes_and_failures() -> None:
    """探针是「不中断」的判据本身，所以它自己必须被测：成功算成功，连不上算失败，
    并且**出口码**就是判据（0 = 一次没掉）。"""
    probe = _load_module("traffic_probe_under_test", PROBE_PATH)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/health"
    try:
        alive = probe.request_once(url)
        assert alive.ok is True
        assert alive.status == 200

        summary = probe.run(url, seconds=0.2, workers=2).summary()
        assert summary["total"] > 0
        assert summary["failed"] == 0
        assert summary["statuses"] == {"200": summary["total"]}

        assert probe.main(["--target", url, "--seconds", "0.2", "--workers", "2"]) == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # 端口已释放：失败必须被记成「连接层失败」，而不是抛异常打断探针。
    dead = probe.request_once(url, timeout=1.0)
    assert dead.ok is False
    assert dead.status is None
    assert dead.detail
    assert probe.main(["--target", url, "--seconds", "0.05", "--timeout", "0.5"]) == 1


def test_probe_reads_the_request_body_from_a_file(tmp_path: Path) -> None:
    """请求体走文件：Windows 的 shell 会把 ``--execute`` 里的内嵌双引号吃掉
    （JSON 到进程里变成 ``{tool:echo}``），验收脚本因此一律用 ``--execute-file``。"""
    probe = _load_module("traffic_probe_body_source", PROBE_PATH)
    body = tmp_path / "probe-body.json"
    body.write_text('{"tool":"echo","arguments":{"message":"hi"}}', encoding="utf-8")

    assert probe._load_body(None, str(body)) == '{"tool":"echo","arguments":{"message":"hi"}}'
    assert probe._load_body("{}", None) == "{}"
    with pytest.raises(SystemExit):
        probe._load_body("{}", str(body))
