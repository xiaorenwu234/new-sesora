"""主动策略对比基线（方案 §9.3 / RQ3）。

与 EGV 相同的候选池与安全过滤，只替换选择策略：
Random / GraphCentrality / FixedPriority / LLM-only。
接口与 ExperimentSelector 一致，可直接注入 pipeline。
"""
from __future__ import annotations

import random

import networkx as nx

from common.models import Budget

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
ACTION_ORDER = {"NETWORK_DELAY": 0, "NETWORK_LOSS": 1, "DEPENDENCY_UNAVAILABLE": 2,
                "POD_TERMINATION": 3, "CPU_PRESSURE": 4, "MEMORY_PRESSURE": 5,
                "LOAD_VARIATION": 6, "OBSERVATION_ONLY": 7, "CONTROLLED_REDEPLOYMENT": 8}


class _BaseSelector:
    """共用预算约束与留痕；子类实现 _score（越大越优先）。"""

    def __init__(self, graph, budget: Budget, history: dict | None = None,
                 attempts_by_key: dict | None = None):
        self.graph = graph
        self.budget = budget
        self.attempts_by_key = attempts_by_key or {}
        self.ranking_log: list[dict] = []

    def _score(self, cand: dict, hypotheses_by_id: dict) -> float:
        raise NotImplementedError

    def rank(self, candidates: list[dict], hypotheses_by_id: dict) -> list[dict]:
        ranked = [c | {"utility": round(self._score(c, hypotheses_by_id), 4)}
                  for c in candidates]
        ranked.sort(key=lambda x: x["utility"], reverse=True)
        self.ranking_log.append({"candidates": ranked})
        return ranked

    def select(self, candidates: list[dict], hypotheses_by_id: dict) -> dict | None:
        if not candidates or not self.budget.available():
            return None
        best = self.rank(candidates, hypotheses_by_id)[0]
        if best["estimated_cost"] > self.budget.max_wall_time_s - self.budget.used_wall_time_s:
            return None
        return best

    def record_attempt(self, cand: dict):
        key = (cand["hypothesis_id"], cand["action_type"], cand["fault_target"])
        self.attempts_by_key[key] = self.attempts_by_key.get(key, 0) + 1


class RandomSelector(_BaseSelector):
    def __init__(self, *a, seed: int = 42, **kw):
        super().__init__(*a, **kw)
        self.rng = random.Random(seed)

    def _score(self, cand, hypotheses_by_id):
        return self.rng.random()


class CentralitySelector(_BaseSelector):
    """按观测对象在依赖图中的介数中心性排序（结构重要性启发式）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        dg = self.graph.service_dependency_graph()
        self.centrality = nx.betweenness_centrality(dg)

    def _score(self, cand, hypotheses_by_id):
        return self.centrality.get(cand["observe_target"], 0.0)


class FixedPrioritySelector(_BaseSelector):
    """固定规则：契约严重度 > 动作侵入性（先低破坏动作）。"""

    def _score(self, cand, hypotheses_by_id):
        h = hypotheses_by_id.get(cand["hypothesis_id"])
        sev = SEVERITY_ORDER.get(getattr(h, "severity", "medium"), 2) \
            if h is not None else 2
        # 假设权重已含严重度，这里直接用权重 + 动作序
        w = h.weight if h is not None else 0.5
        return w * 10 - ACTION_ORDER.get(cand["action_type"], 9) * 0.1 - sev * 0.01


class LLMOnlySelector(_BaseSelector):
    """LLM 直接排序（无显式效用分解）；无 key 时回退固定优先级。"""

    def __init__(self, *a, llm=None, **kw):
        super().__init__(*a, **kw)
        self.llm = llm
        self._fallback = FixedPrioritySelector(*a, **kw)

    def select(self, candidates, hypotheses_by_id):
        if not candidates or not self.budget.available():
            return None
        if self.llm is None or not self.llm.available():
            return self._fallback.select(candidates, hypotheses_by_id)
        import json
        brief = [{"i": i, "action": c["action_type"], "fault": c["fault_target"],
                  "observe": c["observe_target"],
                  "hypothesis": hypotheses_by_id[c["hypothesis_id"]].statement[:120]}
                 for i, c in enumerate(candidates)]
        out = self.llm.complete_json(
            "你是混沌实验选择专家。只能从给定候选中选择，不得发明新动作。",
            json.dumps(brief, ensure_ascii=False), '{"best_index": 0, "reason": "..."}')
        idx = (out or {}).get("best_index", 0)
        best = candidates[idx] if isinstance(idx, int) and 0 <= idx < len(candidates) \
            else candidates[0]
        self.ranking_log.append({"llm_choice": best, "reason": (out or {}).get("reason")})
        return best


STRATEGIES = {"random": RandomSelector, "centrality": CentralitySelector,
              "fixed_priority": FixedPrioritySelector, "llm_only": LLMOnlySelector}