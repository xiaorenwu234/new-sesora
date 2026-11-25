"""Experiment Planner：把选中的候选动作落成完整实验计划（不执行）。

Oracle 阈值一律绑定冻结 SLO；LLM 只能在安全范围内微调故障参数，
不能更改判定标准（方案 §4.1 关键约束）。
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from common.models import Experiment

SYSTEM = """你是混沌实验计划专家。给定候选动作与契约，输出实验参数建议。
约束：不得修改 Oracle 阈值；故障时长不得超过给定上限；只能使用给定的动作类型。"""


class ParamAdvice(BaseModel):
    params: dict = Field(default_factory=dict,
                         description="调整后的故障参数，键值必须在给定参数边界内")
    rationale: str = Field(default="", description="调整理由；参数已合理时说明原样保留")

PARAM_BOUNDS = {
    "time_ms": (500, 10000), "percent": (10, 100),
    "cpu_percent": (30, 100), "mem_percent": (30, 95),
    "evict_count": (1, 1), "duration_s": (10, 180),
    # 只读深挖参数
    "lookback_s": (60, 3600), "tail_lines": (50, 2000),
    "limit": (1, 50), "max_steps": (1, 8),
    "min_duration_ms": (0, 60000), "max_lines": (50, 400),
}


class ExperimentPlanner:
    def __init__(self, llm, tools, cfg):
        self.llm = llm
        self.tools = tools
        self.cfg = cfg

    def plan(self, candidate: dict, hypothesis, contract, slo) -> Experiment:
        params = dict(candidate.get("params", {}))
        if self.llm.available():
            prompt = (
                f"动作: {candidate['action_type']}\n故障对象: {candidate['fault_target']}\n"
                f"观测对象: {candidate['observe_target']}\n契约: {contract.id}\n"
                f"当前参数: {json.dumps(params)}\n参数边界: {json.dumps(PARAM_BOUNDS)}\n"
                "如当前参数已合理，原样返回；否则给出边界内的调整。")
            out = self.llm.complete_structured(SYSTEM, prompt, ParamAdvice)
            if out and isinstance(out.get("params"), dict):
                params = self._clamp(out["params"], params)

        x = Experiment(
            hypothesis_id=hypothesis.hypothesis_id,
            action_type=candidate["action_type"],
            target=candidate["fault_target"],
            fault_target=candidate["fault_target"],
            observe_target=candidate["observe_target"],
            params=params,
            workload={"type": self.cfg.workload.get("locust_file", "locust"),
                      "users": self.cfg.workload.get("users", 20),
                      "scenario": contract.workload},
            oracle={"type": contract.oracle.get("type"),
                    "slo_file": self.cfg.get("slo_file"),
                    "thresholds": contract.oracle_thresholds(slo, candidate["observe_target"])},
            expected_evidence=candidate.get("expected_evidence", []),
            estimated_cost=candidate.get("estimated_cost", 0),
            estimated_risk=candidate.get("estimated_risk", 0),
            utility=candidate.get("utility", 0),
            run_id=self.tools.graph.run_id)
        # 计划提交给控制器队列（审计留痕），执行由控制器负责
        self.tools.submit_verification_plan(
            {"experiment_id": x.experiment_id, "action": x.action_type,
             "fault_target": x.fault_target, "observe_target": x.observe_target})
        return x

    def _clamp(self, proposed: dict, default: dict) -> dict:
        out = dict(default)
        for k, v in proposed.items():
            if k in PARAM_BOUNDS and isinstance(v, (int, float)):
                lo, hi = PARAM_BOUNDS[k]
                out[k] = max(lo, min(hi, v))
        return out