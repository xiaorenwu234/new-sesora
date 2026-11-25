"""检测效果指标（RQ1）：对冻结 Ground Truth 计算 Precision/Recall/F1/覆盖率。

Ground Truth 与评估实现完全独立维护（benchmarks/ground_truth/*.yaml），
每条含 rationale 与 verification 来源，评审前标记 pending_review。
"""
from __future__ import annotations

from pathlib import Path

import yaml


def load_ground_truth(path: str | Path) -> list[dict]:
    entries = yaml.safe_load(Path(path).read_text()) or []
    return [e for e in entries if e.get("status") != "pending_review"]


def finding_keys(report: dict) -> set[tuple]:
    """报告中的确定性判定 (contract_id, target, state)。"""
    out = set()
    for section in ("compliance_findings", "validated_runtime_findings", "compliant"):
        for f in report.get(section, []):
            out.add((f["contract_id"], f["target"], f["state"]))
    return out


def evaluate(report: dict, ground_truth: list[dict]) -> dict:
    gt_defects = {(e["contract_id"], e["target"]) for e in ground_truth
                  if e["expected_state"] == "VIOLATED"}
    gt_compliant = {(e["contract_id"], e["target"]) for e in ground_truth
                    if e["expected_state"] == "COMPLIANT"}
    keys = finding_keys(report)
    pred_defects = {(c, t) for c, t, s in keys if s == "VIOLATED"}
    pred_compliant = {(c, t) for c, t, s in keys if s == "COMPLIANT"}
    decided = pred_defects | pred_compliant

    tp = len(pred_defects & gt_defects)
    fp = len(pred_defects - gt_defects)
    fn = len(gt_defects - pred_defects)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    # 误判合规（把真实缺陷判为 COMPLIANT）单独报告，比未判定更严重
    false_compliant = len(pred_compliant & gt_defects)
    gt_all = gt_defects | gt_compliant
    return {
        "ground_truth_entries": len(ground_truth),
        "tp": tp, "fp": fp, "fn": fn,
        "precision": round(precision, 4), "recall": round(recall, 4),
        "f1": round(f1, 4),
        "false_compliant": false_compliant,
        "coverage_of_gt": round(len(decided & gt_all) / len(gt_all), 4) if gt_all else 0.0,
        "missed_defects": sorted(gt_defects - pred_defects),
        "false_positives": sorted(pred_defects - gt_defects),
        "distinct_issues": len(pred_defects),
    }


def compare(ours: dict, baseline: dict, ground_truth: list[dict],
            baseline_supported: set[tuple] | None = None) -> dict:
    """Comparison A（共同适用范围）与 Comparison B（完整集合覆盖）。"""
    m_ours = evaluate(ours, ground_truth)
    m_base = evaluate(baseline, ground_truth)
    common_gt = [e for e in ground_truth
                 if baseline_supported is None or
                 (e["contract_id"], e["target"]) in baseline_supported]
    unsupported = [e for e in ground_truth if e not in common_gt]
    return {
        "comparison_a": {"ours": evaluate(ours, common_gt),
                         "baseline": evaluate(baseline, common_gt),
                         "common_scope_entries": len(common_gt)},
        "comparison_b": {"ours": m_ours, "baseline": m_base,
                         "baseline_unsupported": [
                             {"contract_id": e["contract_id"], "target": e["target"]}
                             for e in unsupported]},
    }