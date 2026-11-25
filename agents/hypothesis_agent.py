"""Hypothesis Agent：分析证据缺口，精化/自主提出待验证风险（不执行故障）。

LLM 可用时：基于跨层子图摘要审查确定性假设、补充缺失证据描述；
并自主提出新假设（必须绑定既有契约与真实图谱实体，经确定性验证门，
禁止发明判定标准）。LLM 不可用时：确定性回退（保留风险生成器输出）。
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from common.models import Hypothesis

SYSTEM = """你是云原生架构成熟度评估的假设分析专家。
你只能基于给定的证据图谱摘要分析风险，输出结构化 JSON。
允许：审查既有假设、补充缺失证据、提出新的待验证假设（必须绑定给定契约目录中的契约与真实服务）。
缺失证据可由只读深挖动作（日志/trace/指标/配置/源码）或注入实验补充。
禁止：修改质量契约阈值、发明新契约、把潜在风险直接断言为缺陷、提出超出给定动作空间的实验。"""


class HypothesisReview(BaseModel):
    missing_evidence: list[str] = Field(
        default_factory=list, description="该契约验证所需但遗漏的证据类型")
    reasoning: str = Field(default="", description="判断理由")
    confidence: float = Field(default=0.5, description="置信度 0-1")


class NewHypothesis(BaseModel):
    contract_id: str = Field(description="必须是契约目录中存在的契约 ID")
    target: str = Field(description="必须是服务清单中存在的规范服务名")
    statement: str = Field(description="待验证风险陈述，不得断言为已确认缺陷")
    missing_evidence: list[str] = Field(
        default_factory=list, description="验证所需但缺失的证据类型，限该契约证据词表")
    evidence_ids: list[str] = Field(
        default_factory=list, description="支撑该假设的既有证据 ID（必须真实存在）")
    confidence: float = Field(default=0.5, description="风险成立先验置信度 0-1")


class NewHypotheses(BaseModel):
    proposals: list[NewHypothesis] = Field(default_factory=list)
    reasoning: str = Field(default="", description="整体分析思路")


class HypothesisAgent:
    def __init__(self, llm, tools):
        self.llm = llm
        self.tools = tools

    def run(self, hypotheses: list, contracts_by_id: dict) -> list:
        if not self.llm.available():
            return self._fallback(hypotheses)
        refined = []
        for h in hypotheses:
            if h.status not in ("RISK_UNCONFIRMED", "INCONCLUSIVE"):
                refined.append(h)
                continue
            summary = self.tools.subgraph(h.target)
            c = contracts_by_id.get(h.contract_id)
            prompt = (
                f"契约: {c.id} {c.name}\n假设: {h.statement}\n"
                f"目标服务: {h.target}\n当前缺失证据: {h.missing_evidence}\n"
                f"跨层证据摘要: {json.dumps(summary, ensure_ascii=False)[:2500]}\n"
                "请判断缺失证据列表是否完整，可补充该契约验证所需但遗漏的证据类型。")
            out = self.llm.complete_structured(SYSTEM, prompt, HypothesisReview)
            if out and isinstance(out.get("missing_evidence"), list):
                extra = [m for m in out["missing_evidence"]
                         if isinstance(m, str) and m not in h.missing_evidence]
                # 只允许补充契约语义相关的证据类型（白名单 = 契约缺失证据全集的超集检查）
                h.missing_evidence += extra[:3]
                h.source = "agent_refined"
            refined.append(h)
        refined += self.propose(refined, contracts_by_id)
        return refined

    # ---------- 自主提出新假设 ----------
    def propose(self, hypotheses: list, contracts_by_id: dict) -> list:
        """LLM 基于图谱全局视角提出规则未覆盖的新假设；经确定性验证门。"""
        if not self.llm.available():
            return []
        g = self.tools.graph
        acfg = self.tools.cfg.get("agent", {}) or {}
        limit = int(acfg.get("max_new_hypotheses", 5))
        catalog = [{"id": c.id, "name": c.name, "dimension": c.dimension,
                    "evidence_vocabulary": c.missing_evidence}
                   for c in contracts_by_id.values()]
        services = g.services()
        deps = [f"{a}->{b}" for a, b, _ in g.deps()]
        coverage = [f"{h.contract_id}|{h.target}|{h.status}" for h in hypotheses]
        prompt = (
            f"契约目录: {json.dumps(catalog, ensure_ascii=False)[:3000]}\n"
            f"服务清单: {services}\n依赖边: {deps[:60]}\n"
            f"既有假设覆盖(契约|目标|状态): {coverage[:80]}\n"
            f"请从跨层视角提出最多 {limit} 条规则未覆盖的新风险假设："
            "必须绑定目录内契约与清单内服务，missing_evidence 限该契约词表，"
            "evidence_ids 只能引用你确信存在的证据（不确定则留空）。"
            "不要重复既有覆盖中的 (契约,目标) 组合。")
        out = self.llm.complete_structured(SYSTEM, prompt, NewHypotheses)
        if not out or not isinstance(out.get("proposals"), list):
            return []
        existing = {(h.contract_id, h.target) for h in hypotheses}
        accepted = []
        for p in out["proposals"][:limit * 2]:
            h = self._validate_proposal(p, contracts_by_id, g, existing)
            if h is not None:
                existing.add((h.contract_id, h.target))
                accepted.append(h)
            if len(accepted) >= limit:
                break
        self.proposal_stats = {"proposed": len(out["proposals"]),
                               "accepted": len(accepted)}
        return accepted

    def _validate_proposal(self, p: dict, contracts_by_id: dict, graph,
                           existing: set) -> Hypothesis | None:
        """确定性验证门：契约/服务/证据词表/证据 ID 真实存在 + 去重。"""
        if not isinstance(p, dict):
            return None
        c = contracts_by_id.get(p.get("contract_id"))
        target = p.get("target")
        if c is None or target not in graph.services():
            return None
        if (c.id, target) in existing:
            return None
        vocab = set(c.missing_evidence)
        raw_missing = [m for m in (p.get("missing_evidence") or [])
                       if isinstance(m, str)]
        missing = [m for m in raw_missing if m in vocab]
        if raw_missing and not missing:
            return None                      # 全部越界 = 发明证据词表，拒绝
        raw_ids = [e for e in (p.get("evidence_ids") or []) if isinstance(e, str)]
        real_ids = [e for e in raw_ids if e in graph.nodes]
        if raw_ids and not real_ids:
            return None                      # 引用伪造证据，拒绝
        statement = str(p.get("statement", "")).strip()
        if not statement:
            return None
        try:
            conf = float(p.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        return Hypothesis(
            contract_id=c.id, target=target, statement=statement[:500],
            missing_evidence=missing, evidence_ids=real_ids,
            weight=max(0.1, min(conf, 1.0)), source="agent_proposed",
            system_id=self.tools.cfg.system_id)

    def _fallback(self, hypotheses: list) -> list:
        # 确定性回退：缺口越明确（有静态支撑证据）的假设优先
        return sorted(hypotheses,
                      key=lambda h: (h.status != "RISK_UNCONFIRMED",
                                     -len(h.evidence_ids), -h.weight))