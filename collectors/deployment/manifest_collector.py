"""Git/Helm 渲染后 Manifest 采集器：DESIRED_CONFIG 证据。"""
from __future__ import annotations

from pathlib import Path

import yaml

from common.config import Config, git_commit
from common.models import Evidence
from collectors.deployment._extract import (extract_deployment, extract_service,
                                            extract_hpa)


def collect(cfg: Config, profile: dict) -> dict:
    mdir = Path(cfg.kubernetes.manifest_dir)
    commit = cfg.source.get("commit") or git_commit(cfg.source.repo_path)
    evs: list[Evidence] = []
    docs = []
    for f in sorted(mdir.glob("*.yaml")):
        if f.name in ("kustomization.yaml",):
            continue
        for doc in yaml.safe_load_all(f.read_text()):
            if not isinstance(doc, dict) or "kind" not in doc:
                continue
            docs.append(doc)
            kind = doc["kind"]
            if kind == "Deployment":
                evs += extract_deployment(doc, "DESIRED_CONFIG", cfg.system_id, commit,
                                          f"manifest:{f.name}")
            elif kind == "Service":
                evs += extract_service(doc, "DESIRED_CONFIG", cfg.system_id, commit,
                                       f"manifest:{f.name}")
            elif kind == "HorizontalPodAutoscaler":
                evs += extract_hpa(doc, "DESIRED_CONFIG", cfg.system_id, commit,
                                   f"manifest:{f.name}")
    return {"evidence": evs, "manifest_docs": docs}