"""证据缺口价值估计（EGV 核心，自研）。

V(a|E) = Σ_h w_h · P(resolve h | a, E)

P(resolve) 采用可解释启发式分解：
  coverage   —— 动作能补充的缺失证据比例（证据缺口驱动）
  efficacy   —— 动作类型历史有效率（反馈校准的先验）
  observability —— 观测对象遥测可用性（缺遥测则结论不可靠）
  novelty    —— 同类动作重复尝试的边际递减

后续可在 Benchmark 数据充足时替换为校准概率模型（接口不变）。
"""
from __future__ import annotations

from common.models import READONLY_ACTIONS

# 动作类型初始有效率先验（无历史数据时）
DEFAULT_EFFICACY = {
    "NETWORK_DELAY": 0.85, "NETWORK_LOSS": 0.85,
    "DEPENDENCY_UNAVAILABLE": 0.9, "POD_TERMINATION": 0.9,
    "CPU_PRESSURE": 0.7, "MEMORY_PRESSURE": 0.7,
    "LOAD_VARIATION": 0.75, "CONTROLLED_REDEPLOYMENT": 0.7,
    "OBSERVATION_ONLY": 0.45,   # 只读查询通常不足以确认故障
    # 只读数据深挖：定向取证能力强于泛化观测，但无故障注入判别力
    "LOG_DEEP_DIVE": 0.6, "TRACE_DRILLDOWN": 0.6,
    "METRIC_DRILLDOWN": 0.55, "CONFIG_AUDIT": 0.55,
    "SOURCE_INSPECTION": 0.55,
}


class ValueEstimator:
    def __init__(self, graph, history: dict | None = None):
        """history: {action_type: {"resolved": n, "attempts": n}}（反馈更新器维护）"""
        self.graph = graph
        self.history = history or {}

    def efficacy(self, action_type: str) -> float:
        h = self.history.get(action_type)
        prior = DEFAULT_EFFICACY.get(action_type, 0.6)
        if not h or h.get("attempts", 0) == 0:
            return prior
        # Beta(1,1) 平滑融合先验与历史
        a, n = h.get("resolved", 0), h["attempts"]
        return (a + 2 * prior) / (n + 2)

    def observability(self, observe_target: str) -> float:
        g = self.graph
        if g.evidence(subject=observe_target, kind="metric_window", layer="runtime"):
            return 1.0
        if g.evidence(kind="trace_coverage", layer="runtime"):
            return 0.7   # 有全局 Trace 但无该服务窗口
        return 0.4       # 遥测缺失：实验可能无法给出判别结论

    def fault_observability(self, fault_target: str) -> float:
        """故障对象遥测可用性：生效验证需要故障侧或上游信号，
        故障侧无遥测时只能依赖上游间接证据，判别力下降。"""
        if self.graph.evidence(subject=fault_target, kind="metric_window", layer="runtime"):
            return 1.0
        return 0.6

    def p_resolve(self, candidate: dict, hypothesis, attempts: int = 0) -> float:
        missing = set(hypothesis.missing_evidence)
        provides = set(candidate.get("provides", []))
        if missing:
            coverage = len(missing & provides) / len(missing)
        else:
            coverage = 0.5
        if coverage == 0:
            return 0.0
        eff = self.efficacy(candidate["action_type"])
        obs = self.observability(candidate["observe_target"])
        if candidate["action_type"] not in READONLY_ACTIONS:
            obs *= self.fault_observability(candidate["fault_target"])
        novelty = 0.5 ** attempts
        return round(coverage * eff * obs * novelty, 4)

    def value(self, candidate: dict, hypotheses_by_id: dict,
              attempts_by_key: dict | None = None) -> tuple[float, dict]:
        """返回 (V, 解释)。一个动作可能同时服务多个假设（同故障对象/观测对象）。"""
        attempts_by_key = attempts_by_key or {}
        total, explain = 0.0, []
        for hid, h in hypotheses_by_id.items():
            if h.status not in ("RISK_UNCONFIRMED", "INCONCLUSIVE"):
                continue
            if not _compatible(candidate, h, self.graph):
                continue
            key = (hid, candidate["action_type"], candidate["fault_target"])
            p = self.p_resolve(candidate, h, attempts_by_key.get(key, 0))
            if p > 0:
                total += h.weight * p
                explain.append({"hypothesis": hid, "w": h.weight, "p_resolve": p})
        return round(total, 4), {"contributions": explain}


def _compatible(candidate: dict, h, graph) -> bool:
    """动作与假设的兼容性：直接绑定，或同观测对象+同契约族的可共享实验。"""
    if candidate["hypothesis_id"] == h.hypothesis_id:
        return True
    if candidate.get("contract_id") == h.contract_id and \
            candidate.get("observe_target") == h.target:
        return True
    # 同一故障对象上的依赖故障实验可同时验证多个上游假设
    if candidate["action_type"] in ("NETWORK_DELAY", "NETWORK_LOSS",
                                    "DEPENDENCY_UNAVAILABLE"):
        downs = graph.downstream(h.target)
        if candidate.get("fault_target") in downs and \
                candidate.get("observe_target") == h.target:
            return True
    return False