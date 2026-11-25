"""Kubernetes Events 采集器：重启、调度失败、扩缩容、探针失败等事件。"""
from __future__ import annotations

from common.config import Config
from common.models import Evidence
from collectors.deployment.k8s_collector import load_apis


def collect(cfg: Config, profile: dict) -> dict:
    core, _, _, _ = load_apis(cfg)
    ns = cfg.kubernetes.namespace
    evs: list[Evidence] = []
    for e in core.list_namespaced_event(ns).items:
        doc = e.to_dict()
        obj = doc.get("involved_object") or {}
        if obj.get("kind") not in ("Pod", "Deployment", "ReplicaSet",
                                   "HorizontalPodAutoscaler", "Node"):
            continue
        subject = obj.get("name", "")
        if obj.get("kind") == "Pod" and "-" in subject:
            subject = subject.rsplit("-", 2)[0]  # pod -> deployment 名推断
        evs.append(Evidence(
            kind="k8s_event", layer="runtime", subject=subject,
            attrs={"reason": doc.get("reason"), "type": doc.get("type"),
                   "message": (doc.get("message") or "")[:300],
                   "count": doc.get("count", 1),
                   "object": f"{obj.get('kind')}/{obj.get('name')}",
                   "last_timestamp": str(doc.get("last_timestamp"))},
            source="k8s_events", method="event listing",
            location=f"event/{doc['metadata']['name']}",
            system_id=cfg.system_id))
    return {"evidence": evs}


def restart_delta(cfg: Config, service: str) -> int:
    """Oracle 用：某 Deployment 当前所有 Pod 的重启总数。"""
    core, _, _, _ = load_apis(cfg)
    total = 0
    for p in core.list_namespaced_pod(cfg.kubernetes.namespace).items:
        owners = p.metadata.owner_references or []
        rs = next((o.name for o in owners if o.kind == "ReplicaSet"), "")
        if rs.rsplit("-", 1)[0] != service:
            continue
        for cs in (p.status.container_statuses or []):
            total += cs.restart_count or 0
    return total