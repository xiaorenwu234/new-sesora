"""Result Analyst：解释实验结果并提出结构化跟进提议（不改变判定状态）。

判定由确定性 Oracle + FeedbackUpdater 完成；本角色生成结果解释，
其 followup 提议经 normalize_proposal 门禁后进入下一轮候选池，
是迭代闭环的真实驱动力（而非仅供报告引用的死输出）。
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

SYSTEM = """你是实验结果分析专家。基于基线/故障/恢复三阶段观测与 Oracle 判定，
输出简洁的结果解释。若结果未定论或暴露新证据缺口，可提出一个结构化跟进实验
（followup，将被实际消费为下一轮候选）；无必要时 followup 置空。
不得推翻 Oracle 判定，不得修改阈值，跟进动作限预定义动作空间；
证据缺口可被只读深挖（LOG_DEEP_DIVE/TRACE_DRILLDOWN/METRIC_DRILLDOWN/CONFIG_AUDIT/SOURCE_INSPECTION）
覆盖时优先建议深挖，深挖不足再建议注入。"""


class FollowupProposal(BaseModel):
    action_type: str = Field(description="预定义动作空间中的动作类型")
    fault_target: str = Field(description="故障施加对象服务名")
    observe_target: str = Field(description="SLO 观测对象服务名")
    params: dict = Field(default_factory=dict, description="边界内的故障参数")
    rationale: str = Field(default="", description="跟进实验要补齐的证据缺口")


class ResultExplanation(BaseModel):
    explanation: str = Field(description="简洁的中文结果解释")
    next_step_suggestion: str = Field(default="", description="下一步验证建议（文字）")
    followup: FollowupProposal | None = Field(
        default=None, description="结构化跟进实验提议；无必要时置空")


class ResultAnalyst:
    def __init__(self, llm):
        self.llm = llm

    def analyze(self, experiment, obs: dict, verdict: dict, hypothesis) -> dict:
        during = obs["during"].metrics if obs.get("during") else {}
        baseline = obs["baseline"].metrics if obs.get("baseline") else {}
        facts = {
            "action": experiment.action_type,
            "fault_target": experiment.fault_target,
            "observe_target": experiment.observe_target,
            "effect_verified": obs["during"].effect_verified if obs.get("during") else False,
            "oracle_state": verdict.get("state"),
            "comparisons": verdict.get("comparisons", []),
            "reasons": verdict.get("reasons", []),
            "baseline_error_rate": baseline.get("error_rate"),
            "during_error_rate": during.get("error_rate"),
            "during_p99_ms": during.get("p99_latency_ms"),
        }
        explanation = None
        followup = None
        if self.llm.available():
            out = self.llm.complete_structured(
                SYSTEM, json.dumps(facts, ensure_ascii=False), ResultExplanation)
            if out:
                explanation = out.get("explanation")
                fu = out.get("followup")
                if isinstance(fu, dict) and fu.get("action_type"):
                    followup = fu
        if not explanation:
            explanation = self._template(facts, hypothesis)
        return {"experiment_id": experiment.experiment_id,
                "hypothesis_id": hypothesis.hypothesis_id,
                "explanation": explanation, "facts": facts,
                "followup": followup}

    def _template(self, f: dict, h) -> str:
        if not f["effect_verified"]:
            return (f"实验未能确认故障生效（{f['action']} @ {f['fault_target']}），"
                    f"假设 {h.hypothesis_id} 保持 INCONCLUSIVE，不计为反驳。")
        state = f["oracle_state"]
        cmps = "; ".join(
            f"{c['metric']}={c['value']} (阈值 {c['threshold']})"
            for c in f["comparisons"][:4])
        if state == "VIOLATED":
            return (f"故障生效期间观测到契约违例：{cmps}。"
                    f"假设 {h.hypothesis_id} 被证实为已验证缺陷。")
        if state == "COMPLIANT":
            return (f"故障生效且观测充分，指标满足契约：{cmps}。"
                    f"假设 {h.hypothesis_id} 的风险被有依据地排除。")
        return f"观测不足以判定（{', '.join(f['reasons'][:3])}），保持 INCONCLUSIVE。"