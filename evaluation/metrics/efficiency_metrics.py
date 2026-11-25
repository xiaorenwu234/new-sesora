"""主动验证效率指标（方案 §10 RQ3）。

固定预算下的发现曲线、曲线下面积（AUC）、单位成本有效发现数、
inconclusive 比例。所有策略/基线共用同一 experiments.jsonl 口径：
每行 {"experiment": {...}, "verdict": {"state": ...}, "candidate": {...}}。

"有效发现"按 (contract_id, observe_target) 去重——同一缺陷的重复实验
不重复计数，反映真实缺陷发现进度而非实验次数。
"""
from __future__ import annotations

import json
from pathlib import Path

# 动态基线无 candidate.contract_id 时按动作回退映射
_ACTION_CONTRACT = {
    "NETWORK_LOSS": "RES-005", "NETWORK_DELAY": "RES-005",
    "DEPENDENCY_UNAVAILABLE": "RES-005", "POD_TERMINATION": "RES-004",
}


def load_records(run_dir: str | Path) -> list[dict]:
    """从 experiments.jsonl 提取逐实验记录（主系统与各基线通用）。"""
    path = Path(run_dir) / "experiments.jsonl"
    if not path.exists():
        return []
    records = []
    for i, line in enumerate(path.read_text().splitlines()):
        if not line.strip():
            continue
        d = json.loads(line)
        x = d.get("experiment", {})
        v = d.get("verdict", {})
        cand = d.get("candidate", {})
        action = x.get("action_type", "")
        cid = cand.get("contract_id") or _ACTION_CONTRACT.get(action, action)
        records.append({
            "index": i,
            "experiment_id": x.get("experiment_id", ""),
            "action": action,
            "fault_target": x.get("fault_target") or x.get("target", ""),
            "observe_target": x.get("observe_target", ""),
            "contract_id": cid,
            "state": v.get("state", x.get("status", "UNKNOWN")),
            "cost_s": x.get("cost_actual") or x.get("estimated_cost") or 0,
            "utility": x.get("utility", 0),
        })
    return records


def discovery_curve(records: list[dict]) -> list[dict]:
    """累积去重有效发现曲线。每点 {index, cumulative_defects, state, cost_s}。"""
    seen, cum, curve = set(), 0, []
    for r in records:
        if r["state"] == "VIOLATED":
            key = (r["contract_id"], r["observe_target"])
            if key not in seen:
                seen.add(key)
                cum += 1
        curve.append({"index": r["index"], "cumulative_defects": cum,
                      "state": r["state"], "cost_s": r["cost_s"]})
    return curve


def _auc(curve: list[dict], budget_n: int) -> float:
    """归一化发现曲线 AUC（x=实验序号/预算, y=累积发现/最终发现）。

    最终发现为 0 时返回 0。值域 [0,1]，越大表示越早发现缺陷。
    """
    if not curve:
        return 0.0
    final = curve[-1]["cumulative_defects"]
    if final == 0:
        return 0.0
    n = max(budget_n, len(curve))
    area, prev_x, prev_y = 0.0, 0.0, 0.0
    for p in curve:
        x = (p["index"] + 1) / n
        y = p["cumulative_defects"] / final
        area += (x - prev_x) * (y + prev_y) / 2
        prev_x, prev_y = x, y
    if prev_x < 1.0:  # 预算未用完，剩余区间按最终值延伸
        area += (1.0 - prev_x) * (1.0 + prev_y) / 2
    return round(area, 4)


def efficiency_metrics(records: list[dict], budget_n: int | None = None) -> dict:
    n = len(records)
    budget_n = budget_n or n
    curve = discovery_curve(records)
    states = [r["state"] for r in records]
    n_violated = states.count("VIOLATED")
    n_inconclusive = states.count("INCONCLUSIVE")
    n_ineffective = states.count("INEFFECTIVE")
    distinct = curve[-1]["cumulative_defects"] if curve else 0
    total_cost = sum(r["cost_s"] for r in records)
    first = next((r for r in records if r["state"] == "VIOLATED"), None)
    return {
        "n_experiments": n,
        "budget_n": budget_n,
        "n_violated": n_violated,
        "n_distinct_defects": distinct,
        "n_inconclusive": n_inconclusive,
        "n_ineffective": n_ineffective,
        "inconclusive_ratio": round(n_inconclusive / n, 4) if n else 0.0,
        "total_cost_s": round(total_cost, 1),
        "effective_per_1000s": round(distinct / total_cost * 1000, 4) if total_cost else 0.0,
        "time_to_first_defect_s": round(first["cost_s"], 1) if first else None,
        "auc": _auc(curve, budget_n),
        "discovery_curve": curve,
    }


def compare_strategies(runs: dict[str, str | Path],
                       budget_n: int | None = None) -> dict:
    """多策略效率对比。runs: {策略名: run_dir}。"""
    table = {}
    for name, run_dir in runs.items():
        recs = load_records(run_dir)
        table[name] = efficiency_metrics(recs, budget_n)
    ranking = sorted(table, key=lambda k: table[k]["auc"], reverse=True)
    return {"budget_n": budget_n, "strategies": table, "auc_ranking": ranking}