"""Deployment/Service/HPA 文档 -> 统一 Evidence 的共享提取逻辑。

DESIRED_CONFIG（Git manifest）与 OBSERVED_CONFIG（集群实际）分别提取，
两份证据并存，不互相覆盖（方案 §4.2 管线 2）。
"""
from __future__ import annotations

from common.models import Evidence


def _ev(kind, subject, attrs, location, view, system_id, version, source, method):
    return Evidence(kind=kind, layer="deployment", subject=subject, attrs=attrs,
                    source=source, method=method, location=location,
                    config_view=view, system_id=system_id, version=version)


def extract_deployment(doc: dict, view: str, system_id: str, version: str,
                       source: str) -> list[Evidence]:
    meta = doc.get("metadata", {})
    name = meta.get("name", "unknown")
    spec = doc.get("spec", {})
    tmpl = spec.get("template", {}).get("spec", {})
    containers = tmpl.get("containers", [])
    loc = f"deployment/{name}"
    method = f"{source}:{view}"
    evs = []

    evs.append(_ev("replica_count", name, {"replicas": spec.get("replicas", 1)},
                   loc, view, system_id, version, source, method))
    evs.append(_ev("update_strategy", name,
                   {"type": spec.get("strategy", {}).get("type", "RollingUpdate"),
                    "rolling_update": spec.get("strategy", {}).get("rollingUpdate", {})},
                   loc, view, system_id, version, source, method))
    evs.append(_ev("termination_configuration", name,
                   {"grace_period_s": tmpl.get("terminationGracePeriodSeconds", 30)},
                   loc, view, system_id, version, source, method))

    for c in containers:
        cname = c.get("name", name)
        evs.append(_ev("container_image", name,
                       {"container": cname, "image": c.get("image", ""),
                        "digest": c.get("imageID", "") if view == "OBSERVED_CONFIG" else ""},
                       loc, view, system_id, version, source, method))
        res = c.get("resources", {}) or {}
        evs.append(_ev("resource_configuration", name,
                       {"container": cname, "requests": res.get("requests", {}),
                        "limits": res.get("limits", {})},
                       loc, view, system_id, version, source, method))
        evs.append(_ev("probe_configuration", name,
                       {"container": cname,
                        "readiness": c.get("readinessProbe"),
                        "liveness": c.get("livenessProbe"),
                        "startup": c.get("startupProbe")},
                       loc, view, system_id, version, source, method))
        evs.append(_ev("lifecycle_configuration", name,
                       {"container": cname, "pre_stop": (c.get("lifecycle") or {}).get("preStop")},
                       loc, view, system_id, version, source, method))
        envs = c.get("env", []) or []
        evs.append(_ev("env_configuration", name,
                       {"container": cname,
                        "env": {e.get("name"): e.get("value", "<ref>") for e in envs},
                        "env_from": [list(x.keys())[0] for x in (c.get("envFrom") or [])]},
                       loc, view, system_id, version, source, method))
        evs.append(_ev("port_configuration", name,
                       {"container": cname,
                        "ports": [p.get("containerPort") for p in (c.get("ports") or [])]},
                       loc, view, system_id, version, source, method))

    vols = tmpl.get("volumes", []) or []
    evs.append(_ev("volume_configuration", name,
                   {"volumes": [{k: v for k, v in v.items() if k != "name"} | {"name": v.get("name")}
                                for v in vols],
                    "host_path": any("hostPath" in v for v in vols),
                    "empty_dir": any("emptyDir" in v for v in vols)},
                   loc, view, system_id, version, source, method))
    evs.append(_ev("service_account", name,
                   {"service_account": tmpl.get("serviceAccountName", "default")},
                   loc, view, system_id, version, source, method))
    evs.append(_ev("labels_annotations", name,
                   {"labels": meta.get("labels", {}),
                    "pod_labels": spec.get("template", {}).get("metadata", {}).get("labels", {}),
                    "annotations": meta.get("annotations", {})},
                   loc, view, system_id, version, source, method))
    return evs


def extract_service(doc: dict, view: str, system_id: str, version: str,
                    source: str) -> list[Evidence]:
    meta = doc.get("metadata", {})
    name = meta.get("name", "unknown")
    spec = doc.get("spec", {})
    return [_ev("service_routing", name,
                {"selector": spec.get("selector", {}),
                 "ports": spec.get("ports", []),
                 "type": spec.get("type", "ClusterIP")},
                f"service/{name}", view, system_id, version, source,
                f"{source}:{view}")]


def extract_hpa(doc: dict, view: str, system_id: str, version: str,
                source: str) -> list[Evidence]:
    meta = doc.get("metadata", {})
    spec = doc.get("spec", {})
    ref = spec.get("scaleTargetRef", {})
    return [_ev("hpa_configuration", ref.get("name", meta.get("name", "unknown")),
                {"min_replicas": spec.get("minReplicas"),
                 "max_replicas": spec.get("maxReplicas"),
                 "metrics": spec.get("metrics", []),
                 "hpa_name": meta.get("name")},
                f"hpa/{meta.get('name')}", view, system_id, version, source,
                f"{source}:{view}")]