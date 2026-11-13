"""阶段 8：滚动替换——把「停一个、补一个」做成可重复的验收脚本。

Compose（非 swarm）没有原生 rolling：``deploy.replicas`` 被忽略，``up -d`` 对已存在的容器是先停后起。
所以「滚动更新不中断」这条验收只能自己写流程，并且**必须一边打流量一边替换**，否则证明不了什么。

脚本做的事（每一步都打印出来）：

1. 可选 ``--build``：先把新版本镜像构建出来（**不重建**正在跑的副本，所以此刻新旧版本并存）；
2. 确保 ``REPLICAS`` 个副本 + gateway 都在跑，等到全部 healthy、gateway 能答 ``/health``；
3. 后台起探针（``deploy/traffic_probe.py``，默认同时打 ``/health`` 与 ``/v1/tools/execute``）；
4. 逐个体替换副本：``docker stop``（优雅，25s 上限）→ ``docker rm`` → ``up -d --scale N`` 补一个新的 → 等健康；
5. 收尾打印探针报告与替换前后的镜像 ID；**探针失败数非 0 就以非 0 退出**。

    python deploy/rollout.py --replicas 3 --probe-seconds 60 --build
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from traffic_probe import (
    ProbeReport,
)
from traffic_probe import (
    run as probe_run,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("-f", "docker-compose.yml", "-f", "docker-compose.scale.yml")
SERVICE = "driver-platform"
DEFAULT_REPLICAS = 2
DEFAULT_PROBE_SECONDS = 45.0
DEFAULT_PROBE_WORKERS = 2
STOP_TIMEOUT_SECONDS = 25  # 比 uvicorn 的 --timeout-graceful-shutdown 20 略大
HEALTH_TIMEOUT_SECONDS = 180.0


def sh(cmd: list[str]) -> str:
    """跑一条命令并返回 stdout；非 0 直接抛（替换流程里任何一步失败都不该继续）。"""
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"命令失败（exit {result.returncode}）：{' '.join(cmd)}\n"
            f"{result.stdout.strip()}\n{result.stderr.strip()}"
        )
    return result.stdout


def compose(*args: str) -> str:
    return sh(["docker", "compose", *COMPOSE_FILES, *args])


def container_ids() -> list[str]:
    return compose("ps", "-q", SERVICE).split()


def health_status(container: str) -> str:
    return sh(["docker", "inspect", "-f", "{{.State.Health.Status}}", container]).strip()


def image_id(container: str) -> str:
    return sh(["docker", "inspect", "-f", "{{.Image}}", container]).strip()[:19]


def wait_healthy(replicas: int, timeout: float = HEALTH_TIMEOUT_SECONDS) -> None:
    """等到**恰好** replicas 个副本都是 healthy。容器数不对也算没就绪（正在替换时的中间态）。"""
    deadline = time.monotonic() + timeout
    statuses: list[str] = []
    while True:
        ids = container_ids()
        statuses = [health_status(container) for container in ids]
        if len(ids) == replicas and all(status == "healthy" for status in statuses):
            print(f"  {len(ids)} 个副本全部 healthy")
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"等待副本健康超时：{len(ids)}/{replicas} 个容器，状态 {statuses}")
        time.sleep(2)


def wait_gateway(port: int, timeout: float = 60.0) -> None:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    print(f"  gateway 就绪：{url}")
                    return
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"等待 gateway 就绪超时：{url}")
        time.sleep(1)


def replace_one(container: str, replicas: int) -> None:
    """停掉一个副本、删掉它、再补一个新的；期间其余副本继续服务。"""
    print(f"  stop {container[:12]}（{image_id(container)}，优雅停机最多 {STOP_TIMEOUT_SECONDS}s）")
    sh(["docker", "stop", "--time", str(STOP_TIMEOUT_SECONDS), container])
    sh(["docker", "rm", container])
    compose("up", "-d", "--no-deps", "--scale", f"{SERVICE}={replicas}", SERVICE)
    wait_healthy(replicas)


def _execute_body() -> tuple[bytes, dict[str, str]]:
    payload = {"tool": "echo", "arguments": {"message": "rollout"}}
    return json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"}


def start_probes(
    port: int, seconds: float, workers: int, mode: str, api_key: str
) -> tuple[list[threading.Thread], list[ProbeReport]]:
    """按 mode 起 1–2 个探针线程（health / execute / mixed），返回线程与报告。"""
    base = f"http://127.0.0.1:{port}"
    reports: list[ProbeReport] = []
    threads: list[threading.Thread] = []

    def spawn(**kwargs: object) -> None:
        report = ProbeReport()
        reports.append(report)
        threads.append(
            threading.Thread(
                target=probe_run,
                kwargs={"seconds": seconds, "workers": workers, "report": report, **kwargs},
                daemon=True,
            )
        )

    if mode in ("health", "mixed"):
        spawn(target=f"{base}/health")
    if mode in ("execute", "mixed"):
        body, headers = _execute_body()
        headers["X-API-Key"] = api_key
        spawn(target=f"{base}/v1/tools/execute", method="POST", body=body, headers=headers)

    for thread in threads:
        thread.start()
    return threads, reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="逐个体替换副本，同时打流量验证不中断")
    parser.add_argument(
        "--replicas", type=int, default=int(os.environ.get("REPLICAS", DEFAULT_REPLICAS))
    )
    parser.add_argument(
        "--probe-seconds",
        type=float,
        default=float(os.environ.get("PROBE_SECONDS", DEFAULT_PROBE_SECONDS)),
    )
    parser.add_argument("--probe-workers", type=int, default=DEFAULT_PROBE_WORKERS)
    parser.add_argument(
        "--probe",
        choices=("health", "execute", "mixed"),
        default="mixed",
        help="探针打什么：/health、/v1/tools/execute，或两者都打",
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("API_KEY", "read-key"), help="execute 探针的凭证"
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("DRIVER_PORT") or 8000))
    parser.add_argument(
        "--build", action="store_true", help="先构建新版本镜像（不重建运行中的副本）"
    )
    parser.add_argument("--skip-up", action="store_true", help="假定整套已经在跑，不再 up")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.build:
        print("1. 构建新版本镜像（运行中的副本暂不动）")
        compose("build", SERVICE)

    if not args.skip_up:
        print(f"2. 起 {args.replicas} 个副本 + gateway（等健康门）")
        compose("up", "-d", "--scale", f"{SERVICE}={args.replicas}")
    else:
        print("2. 跳过 up（--skip-up）")
    wait_healthy(args.replicas)
    wait_gateway(args.port)

    before = {container: image_id(container) for container in container_ids()}
    print("3. 起探针（替换期间持续打流量）")
    threads, reports = start_probes(
        args.port, args.probe_seconds, args.probe_workers, args.probe, args.api_key
    )

    print("4. 逐个体替换副本")
    for container in list(before):
        replace_one(container, args.replicas)

    print("5. 等探针跑完")
    for thread in threads:
        thread.join()

    # summary() 返回 dict[str, object]（它还要带 duration_ms 这种嵌套结构）；
    # 这里只累加四个标量字段，用显式类型 + int() 收窄，别让 object 传染整段代码。
    total = 0
    ok = 0
    failed = 0
    statuses: dict[str, int] = {}
    errors: dict[str, int] = {}
    for report in reports:
        part = report.summary()
        total += part["total"]
        ok += part["ok"]
        failed += part["failed"]
        statuses.update(part["statuses"])
        errors.update(part["errors"])
    summary = {"total": total, "ok": ok, "failed": failed, "statuses": statuses, "errors": errors}

    after = {container: image_id(container) for container in container_ids()}
    changed = sorted(set(before.values())) != sorted(set(after.values()))
    print(f"  替换前镜像：{sorted(set(before.values()))}")
    print(f"  替换后镜像：{sorted(set(after.values()))}（版本是否变化：{changed}）")
    print(
        f"  探针：{summary['total']} 次请求，失败 {summary['failed']} 次，"
        f"状态分布 {summary['statuses']}"
    )
    print(json.dumps(summary, ensure_ascii=False))
    if summary["failed"]:
        print("滚动更新期间有请求失败——不满足「不中断」")
        return 1
    print("滚动更新完成：替换全部副本期间没有一次请求失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
