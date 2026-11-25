"""Kubernetes API 采集器：OBSERVED_CONFIG 证据 + 集群快照。"""
from __future__ import annotations

from kubernetes import client, config as k8s_config

from common.config import Config
from common.models import Evidence
from collectors.deployment._extract import (extract_deployment, extract_service,
                                            extract_hpa)


def load_apis(cfg: Config):
    k8s_config.load_kube_config(config_file=cfg.kubernetes.kubeconfig)
    return (client.CoreV1Api(), client.AppsV1Api(),
            client.AutoscalingV2Api(), client.CustomObjectsApi())


def collect(cfg: Config, profile: dict) -> dict:
    core, apps, autoscaling, _ = load_apis(cfg)
    ns = cfg.kubernetes.namespace
    sid = cfg.system_id
    evs: list[Evidence] = []
    snapshot = {"deployments": {}, "services": {}, "pods": {}, "hpas": {}}
    namespace = f"namespace/{ns}"
    try:
        quotas = core.list_namespaced_resource_quota(ns).items
        evs.append(Evidence(
            kind="namespace_quotas", layer="deployment", subject=namespace,
            attrs={"quotas": [q.metadata.name for q in quotas]}, source="k8s_api",
            method="resource quota listing", location=namespace,
            config_view="OBSERVED_CONFIG", system_id=sid))
    except client.exceptions.ApiException:
        pass

    zones = {}
    try:
        zones = {node.metadata.name: (node.metadata.labels or {}).get("topology.kubernetes.io/zone")
                 for node in core.list_node().items}
    except client.exceptions.ApiException:
        pass

    for d in apps.list_namespaced_deployment(ns).items:
        doc = d.to_dict()
        # to_dict 使用 snake_case，转换回提取器期望的 camelCase 关键字段
        spec = doc["spec"]
        tmpl = spec["template"]["spec"]
        norm = {
            "status": {"readyReplicas": (doc.get("status") or {}).get("ready_replicas") or 0},
            "metadata": {"name": doc["metadata"]["name"],
                         "labels": doc["metadata"].get("labels") or {},
                         "annotations": doc["metadata"].get("annotations") or {}},
            "spec": {
                "replicas": spec.get("replicas", 1),
                "strategy": {"type": (spec.get("strategy") or {}).get("type", "RollingUpdate"),
                             "rollingUpdate": (spec.get("strategy") or {}).get("rolling_update") or {}},
                "template": {"metadata": {"labels": spec["template"]["metadata"].get("labels") or {}},
                             "spec": _norm_pod_spec(tmpl)},
            },
        }
        evs += extract_deployment(norm, "OBSERVED_CONFIG", sid,
                                  doc["metadata"].get("resource_version", ""), "k8s_api")
        snapshot["deployments"][doc["metadata"]["name"]] = {
            "replicas": spec.get("replicas", 1),
            "ready_replicas": (doc.get("status") or {}).get("ready_replicas", 0),
            "labels": spec["template"]["metadata"].get("labels") or {},
            "image": (tmpl.get("containers") or [{}])[0].get("image", ""),
        }

    for s in core.list_namespaced_service(ns).items:
        doc = s.to_dict()
        norm = {"metadata": {"name": doc["metadata"]["name"]},
                "spec": {"selector": doc["spec"].get("selector") or {},
                         "ports": [{"port": p.get("port"), "targetPort": p.get("target_port"),
                                    "nodePort": p.get("node_port"), "name": p.get("name")}
                                   for p in (doc["spec"].get("ports") or [])],
                         "type": doc["spec"].get("type", "ClusterIP")}}
        evs += extract_service(norm, "OBSERVED_CONFIG", sid,
                               doc["metadata"].get("resource_version", ""), "k8s_api")
        snapshot["services"][doc["metadata"]["name"]] = norm["spec"]

    for p in core.list_namespaced_pod(ns).items:
        doc = p.to_dict()
        name = doc["metadata"]["name"]
        statuses = doc.get("status", {}).get("container_statuses") or []
        restarts = sum(cs.get("restart_count", 0) for cs in statuses)
        ready = all(cs.get("ready", False) for cs in statuses) if statuses else False
        node = doc["spec"].get("node_name")
        created = doc["metadata"].get("creation_timestamp")
        conditions = doc.get("status", {}).get("conditions") or []
        ready_since = next((c.get("last_transition_time") for c in conditions
                            if c.get("type") == "Ready" and c.get("status") == "True"), None)
        startup_s = ((ready_since - created).total_seconds()
                     if ready_since and created and restarts == 0 else None)
        evs.append(Evidence(
            kind="pod_status", layer="deployment", subject=_owner_deployment(doc) or name,
            attrs={"pod": name, "phase": doc.get("status", {}).get("phase"),
                   "ready": ready, "restarts": restarts,
                   "node": node, "zone": zones.get(node), "startup_s": startup_s,
                   "image_ids": [cs.get("image_id", "") for cs in statuses]},
            source="k8s_api", method="pod listing", location=f"pod/{name}",
            config_view="OBSERVED_CONFIG", system_id=sid))
        snapshot["pods"][name] = {"owner": _owner_deployment(doc), "ready": ready,
                                  "restarts": restarts,
                                  "labels": doc["metadata"].get("labels") or {}}

    try:
        for h in autoscaling.list_namespaced_horizontal_pod_autoscaler(ns).items:
            doc = h.to_dict()
            spec = doc["spec"]
            norm = {"metadata": {"name": doc["metadata"]["name"]},
                    "spec": {"scaleTargetRef": {"name": spec["scale_target_ref"]["name"]},
                             "minReplicas": spec.get("min_replicas"),
                             "maxReplicas": spec.get("max_replicas"),
                             "metrics": spec.get("metrics") or []}}
            evs += extract_hpa(norm, "OBSERVED_CONFIG", sid,
                               doc["metadata"].get("resource_version", ""), "k8s_api")
            snapshot["hpas"][doc["metadata"]["name"]] = norm["spec"]
    except client.exceptions.ApiException:
        pass

    return {"evidence": evs, "snapshot": snapshot}


def _norm_pod_spec(tmpl: dict) -> dict:
    containers = []
    for c in tmpl.get("containers") or []:
        containers.append({
            "name": c.get("name"), "image": c.get("image"),
            "securityContext": _norm_security(c.get("security_context")),
            "resources": c.get("resources") or {},
            "readinessProbe": _norm_probe(c.get("readiness_probe")),
            "livenessProbe": _norm_probe(c.get("liveness_probe")),
            "startupProbe": _norm_probe(c.get("startup_probe")),
            "lifecycle": {"preStop": (c.get("lifecycle") or {}).get("pre_stop")},
            "env": [{"name": e.get("name"), "value": e.get("value")}
                    for e in (c.get("env") or [])],
            "envFrom": [{k: v for k, v in x.items()} for x in (c.get("env_from") or [])],
            "ports": [{"containerPort": p.get("container_port")}
                      for p in (c.get("ports") or [])],
        })
    return {"containers": containers,
            "securityContext": _norm_security(tmpl.get("security_context")),
            "terminationGracePeriodSeconds": tmpl.get("termination_grace_period_seconds", 30),
            "volumes": tmpl.get("volumes") or [],
            "serviceAccountName": tmpl.get("service_account_name", "default")}


def _norm_security(context):
    if not context:
        return {}
    keys = {"run_as_non_root": "runAsNonRoot", "run_as_user": "runAsUser",
            "allow_privilege_escalation": "allowPrivilegeEscalation",
            "read_only_root_filesystem": "readOnlyRootFilesystem"}
    return {keys.get(key, key): value for key, value in context.items() if value is not None}


def _norm_probe(p):
    if not p:
        return None
    return {k: v for k, v in p.items() if v is not None}


def _owner_deployment(pod_doc: dict) -> str | None:
    """Pod -> ReplicaSet -> Deployment 名推断（通过 owner 链名称规则）。"""
    for ref in pod_doc["metadata"].get("owner_references") or []:
        if ref.get("kind") == "ReplicaSet":
            rs = ref.get("name", "")
            return rs.rsplit("-", 1)[0] if "-" in rs else rs
    return None
