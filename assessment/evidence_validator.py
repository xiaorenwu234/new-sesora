"""证据质量校验：Finding 必须满足最低证据契约，否则降级。

- compliance_violation：至少 1 条带来源的确定性证据；
- validated_defect：必须有生效验证的观测 + 充分性判定；
- 不满足者降级为 unconfirmed_risk（防止无证据结论进入报告）。
"""
from __future__ import annotations

from common.models import ContractState as CS


def validate(findings: list, graph) -> list:
    out = []
    for f in findings:
        if f.category == "compliance_violation":
            real = [eid for eid in f.evidence_ids if eid in graph.nodes]
            if not real:
                f.category = "unconfirmed_risk"
                f.state = CS.RISK_UNCONFIRMED.value
                f.summary += " [降级：缺少可回溯证据]"
        elif f.category == "validated_defect":
            ok = False
            for oid in f.observation_ids:
                n = graph.nodes.get(oid, {})
                data = n.get("data") or {}
                if isinstance(data, str):
                    import json
                    try:
                        data = json.loads(data)
                    except json.JSONDecodeError:
                        data = {}
                if data.get("effect_verified") and data.get("phase") == "during":
                    ok = True
                    break
            if not ok:
                f.category = "unconfirmed_risk"
                f.state = CS.RISK_UNCONFIRMED.value
                f.summary += " [降级：缺少生效验证观测]"
        out.append(f)
    return out