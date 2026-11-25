"""EGV 实验选择器（论文核心算法，自研）。

U(a) = V(a|E) - λ·C_norm(a) - μ·R(a)
- 显式计算证据缺口价值，而非 LLM 经验选择；
- 允许 best utility <= 0 时不执行任何实验（关键的停止自由）；
- 输出完整排序与效用分解，保证可解释性（RQ3）。
safe_set() 划定正效用安全集（top-k）供 Agent 终裁；select() 保留
纯算法 top-1 语义（LLM 不可用回退与消融基线）。
"""
from __future__ import annotations

from common.models import Budget
from active_verification.value_estimator import ValueEstimator

COST_NORM_S = 300.0   # 成本归一化基准（5 分钟）


class ExperimentSelector:
    def __init__(self, graph, budget: Budget, history: dict | None = None,
                 attempts_by_key: dict | None = None):
        self.graph = graph
        self.budget = budget
        self.estimator = ValueEstimator(graph, history)
        self.attempts_by_key = attempts_by_key or {}
        self.ranking_log: list[dict] = []

    def rank(self, candidates: list[dict], hypotheses_by_id: dict) -> list[dict]:
        ranked = []
        for cand in candidates:
            v, explain = self.estimator.value(cand, hypotheses_by_id,
                                              self.attempts_by_key)
            c_norm = cand["estimated_cost"] / COST_NORM_S
            r = cand["estimated_risk"]
            u = v - self.budget.lambda_cost * c_norm - self.budget.mu_risk * r
            entry = cand | {"value": v, "cost_norm": round(c_norm, 4),
                            "utility": round(u, 4), "value_explain": explain}
            ranked.append(entry)
        ranked.sort(key=lambda x: x["utility"], reverse=True)
        self.ranking_log.append({"candidates": ranked})
        return ranked

    def select(self, candidates: list[dict], hypotheses_by_id: dict) -> dict | None:
        if not candidates or not self.budget.available():
            return None
        ranked = self.rank(candidates, hypotheses_by_id)
        best = ranked[0]
        if best["utility"] <= 0:
            return None   # 主动决定不执行实验
        if best["estimated_cost"] > self.budget.max_wall_time_s - self.budget.used_wall_time_s:
            return None
        return best

    def safe_set(self, candidates: list[dict], hypotheses_by_id: dict,
                 k: int = 3) -> list[dict]:
        """正效用安全集：utility>0 且成本不超剩余预算的 top-k（按效用降序）。

        Agent 只能在此集合内终裁；集合为空 = 算法认为无值得执行的实验。
        """
        if not candidates or not self.budget.available():
            return []
        ranked = self.rank(candidates, hypotheses_by_id)
        remaining = self.budget.max_wall_time_s - self.budget.used_wall_time_s
        ok = [c for c in ranked
              if c["utility"] > 0 and c["estimated_cost"] <= remaining]
        return ok[:max(1, k)]

    def record_attempt(self, cand: dict):
        key = (cand["hypothesis_id"], cand["action_type"], cand["fault_target"])
        self.attempts_by_key[key] = self.attempts_by_key.get(key, 0) + 1