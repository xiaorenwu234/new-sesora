"""Experiment Proposer：Agent 自主提议候选实验（只生成结构化数据，不执行）。

静态枚举（action_generator.generate）覆盖契约声明的常规动作×目标组合；
本角色让 Agent 提议枚举盲区：传递下游故障对象、跨服务观测对象、
JVM 方法级参数等。提议经 normalize_proposal 确定性门禁规整后与静态
候选同构，统一过安全过滤与 EGV 打分——Agent 有提议自由，无估值特权。

Token 控制：仅对 (a) 静态候选为空 或 (b) INCONCLUSIVE（已有实验未能
定论）的假设发起提议，每轮上限 agent.max_proposals_per_round。
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from active_verification import action_generator as ag

SYSTEM = """你是验证实验设计专家。给定假设、跨层证据摘要与允许动作空间，
提议能补齐证据缺口的候选实验。动作分两类：
- 注入类（NETWORK_*/POD_TERMINATION/*_PRESSURE/JVM_* 等）：施加受控故障验证运行时行为；
- 只读深挖类（LOG_DEEP_DIVE/TRACE_DRILLDOWN/METRIC_DRILLDOWN/CONFIG_AUDIT/SOURCE_INSPECTION）：
  不注入故障，执行时由探索器在只读工具白名单内有界多步深挖（日志/trace/指标/配置/源码），
  成本低风险零，适合先行取证；仅适用于只读可判定的契约（trace 连续性/错误信号/指标存在性）。
策略：证据缺口可被深挖覆盖时优先提议深挖，深挖不足再提议注入。
约束：只能使用给定动作类型；故障对象不得是受保护服务；参数必须在给定边界内；
不得修改 Oracle 阈值。提议只是候选，最终由效用算法与安全集裁决。"""


class ExperimentProposal(BaseModel):
    action_type: str = Field(description="允许动作空间中的动作类型")
    fault_target: str = Field(description="故障施加对象服务名")
    observe_target: str = Field(description="SLO 观测对象服务名")
    params: dict = Field(default_factory=dict, description="边界内的故障参数")
    rationale: str = Field(default="", description="该实验能补齐什么证据缺口")


class ProposalList(BaseModel):
    proposals: list[ExperimentProposal] = Field(default_factory=list)


class ExperimentProposer:
    def __init__(self, llm, tools, cfg):
        self.llm = llm
        self.tools = tools
        self.cfg = cfg

    def propose(self, unresolved: list, static_candidates: list,
                contracts_by_id: dict) -> list[dict]:
        if not self.llm.available():
            return []
        acfg = self.cfg.get("agent", {}) or {}
        limit = int(acfg.get("max_proposals_per_round", 4))
        covered = {c["hypothesis_id"] for c in static_candidates}
        # 仅对静态枚举盲区或已有实验未定论的假设提议（控制 token）
        eligible = [h for h in unresolved
                    if h.hypothesis_id not in covered or h.status == "INCONCLUSIVE"]
        allowed = [a for a in self.cfg.safety.get("allowed_actions", [])
                   if a in ag.ACTION_PROVIDES]
        protected = self.cfg.safety.get("protected_services", [])
        action_info = {a: sorted(ag.ACTION_PROVIDES[a]) for a in allowed}
        hyps_by_id = {h.hypothesis_id: h for h in unresolved}
        out: list[dict] = []
        for h in eligible[:3]:
            c = contracts_by_id.get(h.contract_id)
            if c is None:
                continue
            summary = self.tools.subgraph(h.target)
            prompt = (
                f"假设: {h.statement}\n契约: {c.id} {c.name}\n"
                f"目标服务: {h.target}\n缺失证据: {h.missing_evidence}\n"
                f"当前状态: {h.status}\n"
                f"跨层证据摘要: {json.dumps(summary, ensure_ascii=False)[:2000]}\n"
                f"允许动作及其可提供证据: {json.dumps(action_info, ensure_ascii=False)[:1500]}\n"
                f"受保护服务(禁止作为故障对象): {protected}\n"
                f"参数边界: {json.dumps(_param_bounds())}\n"
                "请提议最多 2 个能补齐上述证据缺口的候选实验。")
            res = self.llm.complete_structured(SYSTEM, prompt, ProposalList)
            if not res or not isinstance(res.get("proposals"), list):
                continue
            for p in res["proposals"][:2]:
                if not isinstance(p, dict):
                    continue
                cand = ag.normalize_proposal(
                    p | {"hypothesis_id": h.hypothesis_id},
                    hyps_by_id, contracts_by_id, self.cfg, self.tools.graph)
                if cand is not None:
                    out.append(cand)
                if len(out) >= limit:
                    return out
        return out


def _param_bounds() -> dict:
    from agents.experiment_planner import PARAM_BOUNDS
    return {k: list(v) for k, v in PARAM_BOUNDS.items()}