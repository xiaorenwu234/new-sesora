"""Optional raw configuration snapshots and audit history for architecture rules."""
from __future__ import annotations

import json
from datetime import datetime

from common.models import Evidence

_SECURITY_FIELDS = ("User",)
_HOST_FIELDS = ("Privileged", "ReadonlyRootfs", "CapAdd", "CapDrop")


def _docker(path, system_id):
    objects = json.loads(path.read_text())
    for item in objects if isinstance(objects, list) else [objects]:
        config, host = item["Config"], item["HostConfig"]
        name = item["Name"].lstrip("/")
        yield Evidence(
            "docker_configuration", "deployment", f"container/{name}",
            {key: config.get(key) for key in _SECURITY_FIELDS}
            | {key: host.get(key) for key in _HOST_FIELDS},
            "docker_inspect", "container configuration snapshot", str(path),
            system_id=system_id)


def _audit(path, namespace, system_id):
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            event = json.loads(line)
            ref = event.get("objectRef", {})
            code = event.get("responseStatus", {}).get("code", 0)
            if (event.get("stage") != "ResponseComplete" or not 200 <= code < 300
                    or ref.get("namespace") != namespace or ref.get("subresource")
                    or event.get("verb") not in {"create", "patch", "update"}
                    or ref.get("resource") not in {"deployments", "statefulsets", "daemonsets"}
                    or not ref.get("name") or not event.get("stageTimestamp")):
                continue
            stamp = datetime.fromisoformat(event["stageTimestamp"].replace("Z", "+00:00")).timestamp()
            actor = event.get("user", {}).get("username", "")
            request = event.get("requestObject")
            body = request if isinstance(request, dict) else {}
            paths = [item.get("path", "") for item in request if isinstance(item, dict)] if isinstance(request, list) else []
            spec = body.get("spec") or {}
            annotations = (body.get("metadata") or {}).get("annotations") or {}
            template_changed = "template" in spec or any(p.startswith("/spec/template") for p in paths)
            rollback = (event.get("annotations", {}).get("deployment.kubernetes.io/rollback") == "true"
                        or "rollback" in str(annotations.get("kubernetes.io/change-cause", "")).lower()
                        or "rollback" in event.get("userAgent", "").lower())
            location = f"{path}:{number}"
            meta = {"timestamp": stamp, "audit_id": event.get("auditID")}
            if spec or paths or event["verb"] == "create":
                yield Evidence("config_change", "deployment", ref["name"],
                               meta | {"manual": bool(actor) and not actor.startswith("system:"),
                                       "rollback": rollback, "created": event["verb"] == "create"},
                               "kubernetes_audit", "successful workload configuration write", location,
                               system_id=system_id)
            if template_changed or rollback or event["verb"] == "create":
                yield Evidence("deployment_history", "deployment", ref["name"],
                               meta | {"rollback": rollback}, "kubernetes_audit",
                               "successful workload rollout write", location, system_id=system_id)


def collect(cfg, profile):
    settings = cfg.get("architecture", {})
    evidence, status = [], {}
    for service, priority in ({s: 0 for s in profile.get("critical_services", [])}
                              | settings.get("service_priorities", {})).items():
        if isinstance(priority, int) and priority >= 0:
            evidence.append(Evidence("service_priority", "source", service, {"priority": priority},
                                     "system_profile", "configured availability priority",
                                     "architecture.service_priorities", system_id=cfg.system_id))
    for key, parser in (("audit_log", _audit), ("docker_inspect", _docker)):
        if not settings.get(key):
            continue
        path = cfg.resolve(settings[key])
        args = (cfg.kubernetes.namespace, cfg.system_id) if key == "audit_log" else (cfg.system_id,)
        try:
            records = list(parser(path, *args))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            status[key] = {"error": str(error)}
        else:
            evidence.extend(records)
            status[key] = {"records": len(records)}
    return evidence, status
