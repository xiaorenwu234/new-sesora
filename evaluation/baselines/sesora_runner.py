"""SESORA 独立基线运行器（方案 §九）。

隔离原则：
- SESORA 在独立目录克隆/运行，本方法不调用其源代码/规则/评分器；
- 双方只共享受测系统与预先冻结的 Ground Truth；
- SESORA 输出（评分/未评估）通过显式映射文件转换为本评估的
  (contract_id, target, state) 三元组，低分不等于已验证缺陷；
- SESORA 依赖阿里云产品数据采集，本地 K8s 无法接入的能力
  必须记录为 unsupported，不得计为检测失败（Comparison B 单独标注）。

运行前提：可访问 SESORA 仓库与其支持的数据源环境（如阿里云 ACK）。
本模块只做编排与结果映射，不复制 SESORA 的任何实现。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

DEFAULT_REPO = "https://github.com/chaosblade-io/cloud-native-maturity-evaluate"


def ensure_repo(workdir: str | Path, repo_url: str = DEFAULT_REPO) -> Path:
    """独立目录克隆（已存在则复用，不进入本方法代码树）。"""
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    repo = wd / "cloud-native-maturity-evaluate"
    if not repo.exists():
        subprocess.run(["git", "clone", "--depth", "1", repo_url, str(repo)],
                       check=True, timeout=600)
    return repo


def run_sesora(repo: Path, run_cmd: list[str], report_path: Path,
               timeout: int = 1800) -> dict:
    """按 SESORA 官方文档给定的命令运行，返回原始报告 dict。

    run_cmd 由使用者按其 README 提供（本模块不猜测其内部 API）。
    """
    subprocess.run(run_cmd, cwd=str(repo), check=True, timeout=timeout)
    return json.loads(report_path.read_text())


def map_report(raw_report: dict, mapping_file: str | Path) -> dict:
    """显式映射：SESORA 指标 -> 质量契约判定。

    mapping YAML 格式（每条必须人工评审后冻结）：
      - sesora_metric: <指标名>
        contract_id: RES-001
        target: frontend
        state_rule: {violated_below: 60}   # 评分低于阈值 -> RISK_UNCONFIRMED
        note: 低分只映射为未确认风险，绝不直接映射为 VIOLATED
    未出现在映射中的 SESORA 指标记入 unmapped；
    映射中声明但 SESORA 无法采集的记入 unsupported。
    """
    mapping = yaml.safe_load(Path(mapping_file).read_text()) or []
    metrics = raw_report.get("metrics") or raw_report.get("scores") or {}
    findings, unsupported, unmapped = [], [], []
    mapped_names = set()
    for m in mapping:
        name = m["sesora_metric"]
        mapped_names.add(name)
        if name not in metrics:
            unsupported.append({"contract_id": m["contract_id"],
                                "target": m["target"], "sesora_metric": name})
            continue
        score = metrics[name]
        score = score.get("score") if isinstance(score, dict) else score
        rule = m.get("state_rule", {})
        if isinstance(score, (int, float)) and "violated_below" in rule:
            # SESORA 无动态验证能力：低分只能映射为未确认风险
            state = "RISK_UNCONFIRMED" if score < rule["violated_below"] else "COMPLIANT"
        else:
            state = "RISK_UNCONFIRMED"
        findings.append({"contract_id": m["contract_id"], "target": m["target"],
                         "state": state, "category": "baseline_mapped",
                         "severity": "medium", "dimension": "",
                         "summary": f"SESORA {name}={score}", "evidence_ids": []})
    for name in metrics:
        if name not in mapped_names:
            unmapped.append(name)
    return {"compliance_findings": [f for f in findings if f["state"] == "VIOLATED"],
            "validated_runtime_findings": [],
            "compliant": [f for f in findings if f["state"] == "COMPLIANT"],
            "unconfirmed_risks": [f for f in findings
                                  if f["state"] == "RISK_UNCONFIRMED"],
            "unsupported": unsupported, "unmapped_metrics": unmapped,
            "supported_pairs": {(f["contract_id"], f["target"]) for f in findings}}