"""实验反馈更新器：观测结果 -> 假设状态 / 证据图谱 / 动作先验。

判定状态迁移规则（确定性，不由 LLM 决定）：
- 注入未生效            -> INCONCLUSIVE（区分无效实验，不算反驳）
- 生效 + Oracle 违例    -> VIOLATED（已验证缺陷）
- 生效 + Oracle 通过    -> COMPLIANT（风险被有依据地排除）
- 生效 + 观测不足       -> INCONCLUSIVE
"""
from __future__ import annotations

from common.models import ContractState as CS, Evidence


class FeedbackUpdater:
    def __init__(self, graph):
        self.graph = graph
        self.history: dict[str, dict] = {}

    def update(self, hypothesis, experiment: dict, observation, verdict: dict):
        action = experiment["action_type"]
        h = self.history.setdefault(action, {"resolved": 0, "attempts": 0})
        h["attempts"] += 1

        effective = observation.effect_verified
        sufficient = verdict.get("sufficient", False)

        if not effective:
            hypothesis.status = CS.INCONCLUSIVE.value
            resolved = False
        elif not sufficient:
            hypothesis.status = CS.INCONCLUSIVE.value
            resolved = False
        elif verdict.get("violated"):
            hypothesis.status = CS.VIOLATED.value
            resolved = True
        else:
            hypothesis.status = CS.COMPLIANT.value
            resolved = True

        if resolved:
            h["resolved"] += 1
            # 已补充的证据从缺口中移除
            provided = set(experiment.get("provides", []))
            hypothesis.missing_evidence = [
                m for m in hypothesis.missing_evidence if m not in provided]

        # 观测摘要作为新证据回写图谱（驱动下一轮分析）
        ev = Evidence(
            kind="experiment_result", layer="runtime", subject=hypothesis.target,
            attrs={"experiment_id": experiment["experiment_id"],
                   "action_type": action,
                   "fault_target": experiment["fault_target"],
                   "effect_verified": effective,
                   "verdict": verdict.get("state"),
                   "metrics": observation.metrics,
                   "reasons": verdict.get("reasons", [])},
            source="oracle_runner", method=verdict.get("oracle_type", ""),
            location=f"experiment/{experiment['experiment_id']}",
            system_id=hypothesis.system_id)
        self.graph.add_evidence(ev)
        hypothesis.evidence_ids.append(ev.evidence_id)
        self.graph.update_hypothesis(hypothesis)
        return hypothesis