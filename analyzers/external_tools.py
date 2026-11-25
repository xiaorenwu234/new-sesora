"""可选外部工具适配：KubeLinter / Conftest（二进制存在时启用）。

自研确定性检查已覆盖核心契约；这两个工具提供额外的部署最佳实践
交叉验证，输出合并为补充证据，不替代契约判定。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from common.config import Config
from common.models import Evidence


def run_kubelinter(cfg: Config) -> list[Evidence]:
    if not shutil.which("kubelinter"):
        return []
    mdir = cfg.kubernetes.manifest_dir
    try:
        proc = subprocess.run(["kubelinter", "lint", "--format", "json", str(mdir)],
                              capture_output=True, text=True, timeout=120)
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return []
    evs = []
    for r in data.get("Reports", []) or []:
        obj = r.get("Object", {}) or {}
        # KubeLinter JSON：名称在 Object.K8sObject.Name，消息在 Diagnostic.Message
        name = (obj.get("K8sObject") or {}).get("Name") or \
            (obj.get("Metadata") or {}).get("Name", "unknown")
        evs.append(Evidence(
            kind="kubelinter_check", layer="deployment", subject=name,
            attrs={"check": r.get("Check"),
                   "message": r.get("Diagnostic", {}).get("Message", ""),
                   "remediation": r.get("Remediation")},
            source="kubelinter", method="kubelinter lint",
            location=f"manifest:{name}", config_view="DESIRED_CONFIG",
            system_id=cfg.system_id))
    return evs


def run_conftest(cfg: Config, policy_dir: str | Path) -> list[Evidence]:
    if not shutil.which("conftest"):
        return []
    policy_dir = Path(policy_dir)
    if not policy_dir.exists():
        return []
    try:
        proc = subprocess.run(
            ["conftest", "test", "--policy", str(policy_dir), "--output", "json",
             str(cfg.kubernetes.manifest_dir)],
            capture_output=True, text=True, timeout=120)
        data = json.loads(proc.stdout or "[]")
    except Exception:
        return []
    evs = []
    for batch in data if isinstance(data, list) else []:
        for f in batch.get("failures", []) or []:
            evs.append(Evidence(
                kind="conftest_policy", layer="deployment", subject="manifests",
                attrs={"msg": f.get("msg"), "metadata": f.get("metadata", {})},
                source="conftest", method="rego policy test",
                location=batch.get("filename", ""), config_view="DESIRED_CONFIG",
                system_id=cfg.system_id))
    return evs