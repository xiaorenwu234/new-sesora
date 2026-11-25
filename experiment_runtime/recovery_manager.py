"""恢复管理器：保证实验后环境回到基线（guarantee_cleanup）。"""
from __future__ import annotations

import time

from common.config import Config
from collectors.deployment.k8s_collector import load_apis


def wait_deployment_ready(cfg: Config, service: str, timeout: float = 240) -> tuple[bool, float]:
    """等待 Deployment 就绪副本数达到期望值。返回 (ready, 耗时秒)。"""
    _, apps, _, _ = load_apis(cfg)
    ns = cfg.kubernetes.namespace
    start = time.time()
    deadline = start + timeout
    while time.time() < deadline:
        try:
            d = apps.read_namespaced_deployment(service, ns)
            desired = d.spec.replicas or 1
            ready = d.status.ready_replicas or 0
            if ready >= desired and (d.status.updated_replicas or 0) >= desired:
                return True, time.time() - start
        except Exception:
            pass
        time.sleep(3)
    return False, time.time() - start


def pod_inventory(cfg: Config, service: str) -> set[str]:
    core, _, _, _ = load_apis(cfg)
    out = set()
    for p in core.list_namespaced_pod(cfg.kubernetes.namespace).items:
        for ref in (p.metadata.owner_references or []):
            if ref.kind == "ReplicaSet" and ref.name.rsplit("-", 1)[0] == service:
                out.add(p.metadata.name)
    return out


def verify_recovery(cfg: Config, service: str, baseline_pods: set[str],
                    expect_churn: bool = False) -> dict:
    """恢复验证：就绪 + （Pod 终止实验时）确认发生了替换。"""
    ready, secs = wait_deployment_ready(cfg, service)
    current = pod_inventory(cfg, service)
    churned = bool(current - baseline_pods) if expect_churn else True
    return {"ready": ready, "ready_time_s": round(secs, 1),
            "pod_churn_observed": churned,
            "pods_before": sorted(baseline_pods), "pods_after": sorted(current),
            "recovered": ready and churned}