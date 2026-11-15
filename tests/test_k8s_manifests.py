"""K8s 交付物的静态检查。

本机没有可用集群（Docker Desktop 的 Kubernetes 没能启动、``kind`` 的二进制下载被网络挡掉），
所以这份清单只能用「读文件 + 断言结构」的方式守门——好处是它不依赖集群，坏处是
**它证明不了集群能接受这些对象**。真在集群里 apply/滚动的那一步，见 README 的已知限制。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import driver_platform

K8S = Path(__file__).resolve().parents[1] / "deploy" / "k8s"
MANIFESTS = sorted(p for p in K8S.glob("*.yaml") if p.name != "kustomization.yaml")


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), path.name
    return document


@pytest.fixture(scope="module")
def documents() -> dict[str, dict[str, Any]]:
    return {path.name: _load(path) for path in MANIFESTS}


def test_every_manifest_declares_kind_and_name(documents: dict[str, Any]) -> None:
    assert documents, "deploy/k8s 下没有清单"
    for name, document in documents.items():
        assert document["apiVersion"], name
        assert document["kind"], name
        assert document["metadata"]["name"], name


def test_kustomization_lists_every_file_that_exists(documents: dict[str, Any]) -> None:
    kustomization = _load(K8S / "kustomization.yaml")
    listed = {Path(item).name for item in kustomization["resources"]}
    assert listed == set(documents), "kustomization 与目录内容不一致（漏列或多列）"


def test_deployment_is_hardened_and_never_serves_before_it_is_ready(
    documents: dict[str, Any],
) -> None:
    deployment = documents["deployment.yaml"]
    spec = deployment["spec"]
    assert spec["replicas"] == 3
    assert spec["strategy"]["rollingUpdate"]["maxUnavailable"] == 0  # 先起新的再停旧的
    pod = spec["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    # 探针必须打 /health：/metrics 可能被 DRIVER_METRICS_PROTECTED 保护起来
    # 端点摘除是异步的：没有 preStop 时，进程先收到 SIGTERM 停收新连接，而 kube-proxy 还在往这个
    # Endpoint 送流量。真集群实测（140 秒 1367 次请求）加 sleep 5 后滚动期间 0 失败。
    pre_stop = container["lifecycle"]["preStop"]["exec"]["command"]
    assert pre_stop[:2] == ["sh", "-c"]
    assert "sleep" in pre_stop[2]
    assert container["readinessProbe"]["httpGet"]["path"] == "/health"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert container["resources"]["limits"]["memory"]
    # 根文件系统只读时，唯一的可写处必须是挂进来的卷
    mounts = {mount["name"] for mount in container["volumeMounts"]}
    assert {"data", "tmp"} <= mounts


def test_api_keys_come_from_a_secret_not_from_the_manifest(
    documents: dict[str, Any],
) -> None:
    container = documents["deployment.yaml"]["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert "value" not in env["DRIVER_API_KEYS"]
    assert env["DRIVER_API_KEYS"]["valueFrom"]["secretKeyRef"]["name"] == "driver-platform-keys"
    assert documents["secret.example.yaml"]["kind"] == "Secret"


def test_image_tag_follows_the_package_version(documents: dict[str, Any]) -> None:
    image = documents["deployment.yaml"]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.endswith(f":{driver_platform.__version__}")
    assert ":latest" not in image
    kustomization = _load(K8S / "kustomization.yaml")
    assert kustomization["images"][0]["newTag"] == driver_platform.__version__


def test_service_pdb_and_hpa_agree_on_three_replicas(documents: dict[str, Any]) -> None:
    service = documents["service.yaml"]
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == 80
    assert service["spec"]["ports"][0]["targetPort"] == "http"

    hpa = documents["hpa.yaml"]
    assert hpa["spec"]["minReplicas"] == 3
    assert hpa["spec"]["maxReplicas"] >= 3
    assert hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 70

    pdb = documents["pdb.yaml"]
    assert pdb["spec"]["minAvailable"] == 2  # 节点驱逐时仍留两个可用副本


def test_selectors_match_between_workloads(documents: dict[str, Any]) -> None:
    labels = documents["deployment.yaml"]["spec"]["template"]["metadata"]["labels"]
    assert documents["service.yaml"]["spec"]["selector"] == labels
    assert documents["pdb.yaml"]["spec"]["selector"]["matchLabels"] == labels
