"""Finding 去重：按根因键合并（根因级别 Unique Validated Findings，RQ1）。"""
from __future__ import annotations


def deduplicate(findings: list) -> list:
    by_key: dict[str, object] = {}
    for f in findings:
        key = f.root_cause_key or f"{f.contract_id}|{f.target}|{f.summary[:80]}"
        if key not in by_key:
            by_key[key] = f
            continue
        keep = by_key[key]
        # 保留验证强度更高者（validated_defect > compliance_violation > unconfirmed）
        rank = {"validated_defect": 3, "compliance_violation": 2,
                "compliance": 1, "unconfirmed_risk": 0}
        if rank.get(f.category, 0) > rank.get(keep.category, 0):
            f.evidence_ids = list(set(f.evidence_ids + keep.evidence_ids))
            by_key[key] = f
        else:
            keep.evidence_ids = list(set(keep.evidence_ids + f.evidence_ids))
            keep.experiment_ids = list(set(keep.experiment_ids + f.experiment_ids))
            keep.observation_ids = list(set(keep.observation_ids + f.observation_ids))
    return list(by_key.values())