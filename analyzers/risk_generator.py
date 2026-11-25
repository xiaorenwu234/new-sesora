"""风险假设生成器（Module C 第二项任务）。

确定性检查无法判定的契约 -> 结构化 Hypothesis（RISK_UNCONFIRMED），
明确列出缺失证据，交给 Agent / EGV 决定如何补充。
绝不把潜在风险直接输出为缺陷 Finding。
"""
from __future__ import annotations

from common.models import ContractState as CS, Hypothesis
from analyzers.compliance_checker import check, SEVERITY_WEIGHT
from analyzers import architecture


def generate(graph, contracts, system_id: str) -> tuple[list, list]:
    """返回 (findings 直接判定结果, hypotheses 待验证风险)。

    findings 元素: (contract, svc, check_result)
    hypotheses 元素: Hypothesis
    """
    direct, hypotheses = [], []
    seen = set()
    for c in contracts:
        targets = architecture.targets(graph, c) if c.static_check in architecture.CHECKS else graph.services()
        for svc in targets:
            props = graph.service_props(svc)
            if props.get("infra"):
                continue
            if c.requires and not set(c.requires) <= graph.service_tags(svc):
                continue
            key = (c.id, svc)
            if key in seen:
                continue
            seen.add(key)

            if c.dynamic_only:
                result = {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": [],
                          "detail": "dynamic-only contract"}
            else:
                result = check(graph, c, svc)

            state = result["state"]
            if state in (CS.VIOLATED.value, CS.COMPLIANT.value) or not c.actions:
                direct.append((c, svc, result))
                continue
            if state == CS.NOT_APPLICABLE.value:
                direct.append((c, svc, result))
                continue

            # RISK_UNCONFIRMED / INCONCLUSIVE -> 假设
            related = []
            for kind in c.static_evidence:
                related += [e["evidence_id"] for e in graph.evidence(subject=svc, kind=kind)
                            if e.get("evidence_id")]
            weight = SEVERITY_WEIGHT.get(c.severity, 0.5)
            if props.get("critical"):
                weight *= 1.2
            h = Hypothesis(
                contract_id=c.id, target=svc,
                statement=c.hypothesis_statement,
                missing_evidence=list(c.missing_evidence),
                evidence_ids=related + result.get("evidence_ids", []),
                weight=round(weight, 3), source="deterministic",
                system_id=system_id)
            hypotheses.append(h)
    return direct, hypotheses
