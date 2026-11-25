"""独立静态分析基线（方案 §9.3）。

直接运行 Semgrep（独立规则集）+ KubeLinter（默认检查）+ Conftest（独立 rego
策略），不经过质量契约引擎与 Evidence Graph。工具输出经显式映射表转换为与
主系统报告同构的 findings，供 detection_metrics 做 Comparison A/B。

映射口径（评审可审计）：
  KubeLinter check        -> 契约
    unset-cpu/memory-requirements -> SCA-002
    no-readiness/liveness-probe, minimum-replicas -> RES-004
    latest-tag            -> DEP-001
    dangling-horizontalpodautoscaler -> SCA-001
  Conftest deny           -> 结构化 {contract, target, msg}（policy/baseline.rego）
  Semgrep rule metadata.contract -> 契约；源码路径经 profile.service_mapping 对齐

未映射的工具检查只记录在 raw 输出中，不计入检测指标（超出评估范围）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from common.config import new_run_dir

_HERE = Path(__file__).resolve().parent

# KubeLinter check -> (contract_id, severity)
KUBELINTER_MAP = {
    "unset-cpu-requirements": ("SCA-002", "medium"),
    "unset-memory-requirements": ("SCA-002", "medium"),
    "no-readiness-probe": ("RES-004", "high"),
    "no-liveness-probe": ("RES-004", "high"),
    "minimum-replicas": ("RES-004", "high"),
    "latest-tag": ("DEP-001", "medium"),
    "dangling-horizontalpodautoscaler": ("SCA-001", "medium"),
}

SEMGREP_SEVERITY = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _load_profile(profile_path: str | None) -> dict:
    if not profile_path:
        return {}
    return yaml.safe_load(Path(profile_path).read_text()) or {}


def _path_to_service(rel_path: str, mapping: dict) -> str | None:
    """源码相对路径 -> 服务名（最长 source_dir 前缀匹配）。"""
    best, best_len = None, -1
    for svc, m in mapping.items():
        sd = (m or {}).get("source_dir", "")
        if sd and rel_path.startswith(sd) and len(sd) > best_len:
            best, best_len = svc, len(sd)
    return best


def _kubelinter(manifest_dir: str) -> tuple[list[dict], list[dict], bool]:
    if not shutil.which("kubelinter"):
        return [], [], False
    p = _run(["kubelinter", "lint", manifest_dir, "--format", "json"])
    if p.returncode not in (0, 1):  # 1 = 有发现
        return [], [], True
    try:
        reports = json.loads(p.stdout).get("Reports") or []
    except json.JSONDecodeError:
        return [], [], True
    findings, raw = [], []
    for r in reports:
        obj = r.get("Object", {})
        name = (obj.get("K8sObject") or {}).get("Name") or \
            (obj.get("Metadata") or {}).get("Name", "unknown")
        check = r.get("Check") or r.get("Diagnostic", {}).get("CheckName", "")
        message = r.get("Diagnostic", {}).get("Message", "")
        raw.append({"check": check, "target": name, "message": message})
        if check in KUBELINTER_MAP:
            cid, sev = KUBELINTER_MAP[check]
            findings.append({"contract_id": cid, "target": name, "state": "VIOLATED",
                             "severity": sev, "source_tool": "kubelinter",
                             "message": f"[{check}] {message}"})
    return findings, raw, True


def _conftest(manifest_dir: str) -> tuple[list[dict], list[dict], bool]:
    if not shutil.which("conftest"):
        return [], [], False
    p = _run(["conftest", "test", manifest_dir, "--policy", str(_HERE / "policy"),
              "--output", "json"])
    if p.returncode not in (0, 1):
        return [], [], True
    try:
        results = json.loads(p.stdout) or []
    except json.JSONDecodeError:
        return [], [], True
    findings, raw = [], []
    for res in results:
        for fail in res.get("failures") or []:
            # conftest 将结构化 deny 拆为 msg(字符串) + metadata(对象)
            meta = fail.get("metadata") or {}
            msg = fail.get("msg")
            raw.append({"file": res.get("filename"), "msg": msg, "metadata": meta})
            if meta.get("contract") and meta.get("target"):
                findings.append({"contract_id": meta["contract"], "target": meta["target"],
                                 "state": "VIOLATED",
                                 "severity": "high" if meta["contract"] == "RES-004" else "medium",
                                 "source_tool": "conftest", "message": str(msg)})
    return findings, raw, True


def _semgrep(repo_path: str, mapping: dict) -> tuple[list[dict], list[dict], bool]:
    if not shutil.which("semgrep"):
        return [], [], False
    src = Path(repo_path) / "src"
    if not src.is_dir():
        src = Path(repo_path)
    p = _run(["semgrep", "scan", "--config", str(_HERE / "rules" / "semgrep_baseline.yaml"),
              "--json", "--metrics=off", "--quiet", str(src)], timeout=900)
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return [], [], True
    findings, raw = [], []
    for r in data.get("results") or []:
        rel = str(Path(r.get("path", "")).relative_to(repo_path)) \
            if r.get("path", "").startswith(repo_path) else r.get("path", "")
        svc = _path_to_service(rel, mapping)
        extra = r.get("extra", {})
        contract = (extra.get("metadata") or {}).get("contract")
        raw.append({"rule": r.get("check_id"), "path": rel, "service": svc,
                    "line": r.get("start", {}).get("line")})
        if contract and svc:
            findings.append({"contract_id": contract, "target": svc, "state": "VIOLATED",
                             "severity": SEMGREP_SEVERITY.get(extra.get("severity"), "medium"),
                             "source_tool": "semgrep",
                             "message": f"{r.get('check_id')} @ {rel}:{r.get('start', {}).get('line')}"})
    return findings, raw, True


def _deployment_names(manifest_dir: str) -> list[str]:
    names = []
    for f in sorted(Path(manifest_dir).glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "Deployment":
                names.append(doc["metadata"]["name"])
    return names


def run_static_baseline(cfg, profile_path: str | None = None,
                        run_id: str | None = None) -> dict:
    """运行独立静态基线，返回与主系统报告同构的 dict 并落盘。"""
    manifest_dir = cfg["kubernetes"]["manifest_dir"]
    repo_path = cfg["source"]["repo_path"]
    profile = _load_profile(profile_path)
    mapping = profile.get("service_mapping", {})

    t0 = time.time()
    kl_f, kl_raw, kl_ok = _kubelinter(manifest_dir)
    ct_f, ct_raw, ct_ok = _conftest(manifest_dir)
    sg_f, sg_raw, sg_ok = _semgrep(repo_path, mapping)

    # 去重 (contract, target)，保留首个来源
    seen, findings = set(), []
    for f in kl_f + ct_f + sg_f:
        key = (f["contract_id"], f["target"])
        if key not in seen:
            seen.add(key)
            findings.append(f)

    # 基线适用范围：映射表契约 × 全部 Deployment（静态检查对每个部署均适用）
    deployments = _deployment_names(manifest_dir)
    supported_contracts = {c for c, _ in KUBELINTER_MAP.values()} | {"RES-001", "RES-002", "RES-003"}
    baseline_supported = sorted((c, d) for c in supported_contracts for d in deployments)

    report = {
        "run_id": run_id or f"static-baseline-{int(t0)}",
        "method": "static_baseline",
        "system_id": profile.get("system_id", "unknown"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
        "tools": {"kubelinter": kl_ok, "conftest": ct_ok, "semgrep": sg_ok},
        "compliance_findings": findings,
        "validated_runtime_findings": [],
        "compliant": [],  # 静态基线无法给出运行期 COMPLIANT 判定
        "baseline_supported": baseline_supported,
        "raw": {"kubelinter": kl_raw, "conftest": ct_raw, "semgrep": sg_raw},
        "raw_counts": {"kubelinter": len(kl_raw), "conftest": len(ct_raw),
                       "semgrep": len(sg_raw)},
    }
    out_dir = new_run_dir(cfg, report["run_id"])
    (out_dir / "static_baseline_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False))
    report["_path"] = str(out_dir / "static_baseline_report.json")
    return report