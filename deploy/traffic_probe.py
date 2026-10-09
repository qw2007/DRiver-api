"""阶段 8：滚动更新探针——把「不中断」变成数字。

验收「滚动更新不中断」需要一个持续打流量的东西：替换副本期间请求**不能被掉**。
这里只用标准库（``urllib`` + 线程池）：``ab`` / ``hey`` / ``k6`` 本机都没装，
而验收要的不是压测报告，是「失败了几次」。

出口码就是判据：``0`` = 一次没掉，非 ``0`` = 掉了几个（数字打印在最后一行 JSON 里）。

    python deploy/traffic_probe.py --target http://127.0.0.1:8000/health --seconds 30
    python deploy/traffic_probe.py --execute '{"tool":"echo","arguments":{"message":"hi"}}' \
        --api-key read-key --seconds 30
    python deploy/traffic_probe.py --execute-file .tmp/probe-body.json --api-key read-key

Windows 上 shell 会吃掉 ``--execute`` 里的内嵌双引号（JSON 到进程里已经变成
``{tool:echo}``），脚本里一律用 ``--execute-file``。
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TARGET = "http://127.0.0.1:8000/health"
DEFAULT_EXECUTE_URL = "http://127.0.0.1:8000/v1/tools/execute"
DEFAULT_SECONDS = 45.0
DEFAULT_WORKERS = 4
DEFAULT_TIMEOUT = 5.0
DEFAULT_EXPECT = 200


@dataclass(frozen=True)
class Attempt:
    """一次请求的结果。``status`` 为 ``None`` 表示连接层就失败了（连不上/超时/重置）。"""

    ok: bool
    status: int | None
    detail: str
    duration_ms: float


def request_once(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    expect: int = DEFAULT_EXPECT,
) -> Attempt:
    """发一次请求；任何异常都变成 ``Attempt``，探针本身绝不因为目标抖动而崩。"""
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            status = response.status
            detail = response.reason or ""
    except urllib.error.HTTPError as exc:  # 有响应但状态码非 2xx
        elapsed = (time.perf_counter() - started) * 1000
        return Attempt(ok=False, status=exc.code, detail=f"HTTP {exc.code}", duration_ms=elapsed)
    except Exception as exc:  # noqa: BLE001 - 探针要分类所有失败，不能漏
        elapsed = (time.perf_counter() - started) * 1000
        return Attempt(ok=False, status=None, detail=type(exc).__name__, duration_ms=elapsed)
    elapsed = (time.perf_counter() - started) * 1000
    return Attempt(ok=status == expect, status=status, detail=detail, duration_ms=elapsed)


@dataclass
class ProbeReport:
    """累计结果。线程之间用它汇总，所以每次写入都要拿锁。"""

    expected: int = DEFAULT_EXPECT
    attempts: list[Attempt] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, attempt: Attempt) -> None:
        with self._lock:
            self.attempts.append(attempt)

    @property
    def total(self) -> int:
        return len(self.attempts)

    @property
    def failed(self) -> int:
        return sum(1 for attempt in self.attempts if not attempt.ok)

    def summary(self) -> dict[str, object]:
        durations = sorted(attempt.duration_ms for attempt in self.attempts)
        statuses = Counter(
            "connection_error" if attempt.status is None else str(attempt.status)
            for attempt in self.attempts
        )
        errors = Counter(attempt.detail for attempt in self.attempts if not attempt.ok)
        return {
            "total": self.total,
            "ok": self.total - self.failed,
            "failed": self.failed,
            "statuses": dict(statuses),
            "errors": dict(errors),
            "duration_ms": {
                "min": min(durations) if durations else None,
                "median": statistics.median(durations) if durations else None,
                "max": max(durations) if durations else None,
            },
        }


def run(
    target: str = DEFAULT_TARGET,
    *,
    seconds: float = DEFAULT_SECONDS,
    workers: int = DEFAULT_WORKERS,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    expect: int = DEFAULT_EXPECT,
    report: ProbeReport | None = None,
) -> ProbeReport:
    """在 ``seconds`` 秒内用 ``workers`` 个线程持续打 ``target``，返回累计报告。"""
    collected = report if report is not None else ProbeReport(expected=expect)
    deadline = time.monotonic() + seconds

    def worker() -> None:
        while time.monotonic() < deadline:
            collected.record(
                request_once(
                    target,
                    method=method,
                    body=body,
                    headers=headers,
                    timeout=timeout,
                    expect=expect,
                )
            )

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return collected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="滚动更新期间的流量探针（失败数即出口码）")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="要打的目标 URL")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS, help="持续时间（秒）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发线程数")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单次请求超时（秒）")
    parser.add_argument("--expect", type=int, default=DEFAULT_EXPECT, help="期望状态码")
    parser.add_argument(
        "--execute",
        default=None,
        metavar="JSON",
        help='改为 POST /v1/tools/execute，参数是请求体 JSON（如 \'{"tool":"echo"}\'）',
    )
    parser.add_argument(
        "--execute-file",
        default=None,
        metavar="PATH",
        help="同上，但从文件读请求体 JSON——Windows 上 shell 引号会把内嵌双引号吃掉，脚本里用这个更稳",
    )
    parser.add_argument("--api-key", default=None, help="POST 时带的 X-API-Key")
    parser.add_argument("--execute-url", default=DEFAULT_EXECUTE_URL, help="执行入口 URL")
    return parser


def _load_body(execute: str | None, execute_file: str | None) -> str | None:
    """把 --execute / --execute-file 收敛成一个请求体字符串（都给或都不给都非法）。"""
    if execute is not None and execute_file is not None:
        raise SystemExit("--execute 与 --execute-file 只能给一个")
    if execute_file is None:
        return execute
    return Path(execute_file).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    body_text = _load_body(args.execute, args.execute_file)
    if body_text is None:
        url, method, body, headers = args.target, "GET", None, {}
    else:
        json.loads(body_text)  # 早失败：请求体不合法就别开跑
        headers = {"Content-Type": "application/json"}
        if args.api_key:
            headers["X-API-Key"] = args.api_key
        url, method, body = args.execute_url, "POST", body_text.encode("utf-8")

    report = run(
        url,
        seconds=args.seconds,
        workers=args.workers,
        method=method,
        body=body,
        headers=headers,
        timeout=args.timeout,
        expect=args.expect,
    )
    summary = report.summary()
    print(
        f"探针结束：{summary['total']} 次请求，失败 {summary['failed']} 次，"
        f"状态分布 {summary['statuses']}"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
