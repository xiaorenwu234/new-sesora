"""Experiment Judge：Agent 在 EGV 正效用安全集内做最终裁决（不执行）。

决策结构：EGV 显式计算效用并划定安全集（top-k），Agent 在集内选择
最值得执行的实验并给出理由，也可显式拒绝执行（choice=0，保留
"不执行自由"）。Agent 无法选择安全集之外的候选，无法修改效用/阈值。

回退：LLM 不可用或输出非法时回退确定性默认——
- fallback="top_utility"：安全集 top-1（等价纯 EGV，离线可复现）；
- fallback="min_cost"：池内最低成本候选（algo=False 消融的合理替代）。
每轮裁决（含回退）追加 run_dir/agent_decisions.jsonl 审计留痕。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import BaseModel, Field

SYSTEM = """你是混沌实验裁决专家。给定效用算法划定的正效用安全集（已按效用降序），
从中选择最值得执行的一个实验，或判断当前不值得执行任何实验。
约束：只能选择集合内候选（1-based 序号）或 0（拒绝执行）；不得修改效用与阈值；
选择应基于证据缺口价值、成本风险与近期实验反馈，而非个人偏好。"""


class JudgeDecision(BaseModel):
    choice: int = Field(description="安全集内 1-based 序号；0 = 拒绝执行任何实验")
    rationale: str = Field(default="", description="裁决理由（拒绝时说明为何不值得执行）")


class ExperimentJudge:
    def __init__(self, llm):
        self.llm = llm

    def decide(self, safe_set: list[dict], hyps_by_id: dict,
               history: dict | None, run_dir=None,
               fallback: str = "top_utility") -> dict | None:
        if not safe_set:
            return None
        if fallback == "min_cost":
            default = min(safe_set, key=lambda c: c.get("estimated_cost", 0))
        else:
            default = safe_set[0]
        decision, rationale, used_fallback = default, "deterministic fallback", True
        if self.llm.available():
            decision, rationale, used_fallback = self._ask(
                safe_set, hyps_by_id, history, default)
        self._audit(run_dir, safe_set, decision, rationale, used_fallback)
        return decision

    def _ask(self, safe_set, hyps_by_id, history, default):
        entries = []
        for i, c in enumerate(safe_set, 1):
            h = hyps_by_id.get(c["hypothesis_id"])
            entries.append({
                "index": i, "action": c["action_type"],
                "fault_target": c["fault_target"],
                "observe_target": c["observe_target"],
                "hypothesis": getattr(h, "statement", "")[:200],
                "contract_id": c.get("contract_id"),
                "utility": c.get("utility"),
                "value_explain": c.get("value_explain"),
                "estimated_cost_s": c.get("estimated_cost"),
                "estimated_risk": c.get("estimated_risk"),
                "source": c.get("source", "static"),
            })
        recent = {a: {"resolved": s.get("resolved", 0),
                      "attempts": s.get("attempts", 0)}
                  for a, s in (history or {}).items()}
        prompt = (
            f"正效用安全集: {json.dumps(entries, ensure_ascii=False)[:3500]}\n"
            f"近期动作反馈(已解决/尝试): {json.dumps(recent, ensure_ascii=False)}\n"
            "请选择最值得执行的实验序号，或 0 拒绝执行，并给出理由。")
        out = self.llm.complete_structured(SYSTEM, prompt, JudgeDecision)
        if out and isinstance(out.get("choice"), int):
            ch = out["choice"]
            if ch == 0:
                return None, str(out.get("rationale", "")), False
            if 1 <= ch <= len(safe_set):
                return safe_set[ch - 1], str(out.get("rationale", "")), False
        return default, "invalid LLM choice; fallback", True

    def _audit(self, run_dir, safe_set, decision, rationale, used_fallback):
        if run_dir is None:
            return
        rec = {
            "ts": time.time(),
            "safe_set": [{"index": i, "action": c["action_type"],
                          "fault_target": c["fault_target"],
                          "observe_target": c["observe_target"],
                          "utility": c.get("utility"),
                          "source": c.get("source", "static")}
                         for i, c in enumerate(safe_set, 1)],
            "chosen": ({"action": decision["action_type"],
                        "fault_target": decision["fault_target"],
                        "observe_target": decision["observe_target"]}
                       if decision else None),
            "declined": decision is None and not used_fallback,
            "rationale": rationale[:500],
            "fallback": used_fallback,
        }
        try:
            p = Path(run_dir) / "agent_decisions.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass