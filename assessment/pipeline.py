"""评估管线：active_assessment 的完整实现（方案 §5.4 / §七）。

阶段：采集 -> 建图 -> 确定性分析 -> 假设生成 -> EGV 选择 ->
受控执行 -> Oracle -> 反馈迭代 -> 报告。
本模块提供步骤函数，供 LangGraph 工作流或直接循环调用。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from common.config import Config, new_run_dir
from common.llm import LLMClient
from common.models import Budget, Finding, ContractState as CS, READONLY_ACTIONS
from contracts.loader import load_contracts, load_slo
from collectors import architecture
from collectors.source import semgrep_adapter, tree_sitter_adapter
from collectors.deployment import k8s_collector, manifest_collector
from collectors.runtime import jaeger_collector, prometheus_collector, event_collector
from analyzers import compliance_checker, graph_analyzer, risk_generator, external_tools
from evidence_graph.graph_builder import EvidenceGraph
from active_verification import action_generator, stopping_policy
from active_verification.experiment_selector import ExperimentSelector
from active_verification.feedback_updater import FeedbackUpdater
from active_verification.value_estimator import _compatible
from agents.tools import AgentTools
from agents.hypothesis_agent import HypothesisAgent
from agents.experiment_planner import ExperimentPlanner
from agents.experiment_proposer import ExperimentProposer
from agents.experiment_judge import ExperimentJudge
from agents.result_analyst import ResultAnalyst
from experiment_runtime.safety_controller import SafetyController
from experiment_runtime import controller
from assessment import finding_deduplication, evidence_validator, report_generator

import yaml


@dataclass
class AssessmentContext:
    cfg: Config
    run_id: str
    run_dir: Path
    profile: dict
    graph: EvidenceGraph = None
    contracts: list = field(default_factory=list)
    contracts_by_id: dict = field(default_factory=dict)
    slo: dict = field(default_factory=dict)
    budget: Budget = None
    llm: LLMClient = None
    tools: AgentTools = None
    hypothesis_agent: HypothesisAgent = None
    planner: ExperimentPlanner = None
    proposer: ExperimentProposer = None
    judge: ExperimentJudge = None
    analyst: ResultAnalyst = None
    safety: SafetyController = None
    feedback: FeedbackUpdater = None
    selector: ExperimentSelector = None
    hypotheses: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    analyses: list = field(default_factory=list)
    pending_proposals: list = field(default_factory=list)  # analyst followup 待消费
    algo_enabled: bool = True      # 消融 algo=False 时跳过 EGV，Agent 直选安全过滤池
    coverage: dict = field(default_factory=dict)     # (cid,svc) -> state
    graph_analysis: dict = field(default_factory=dict)
    collection_meta: dict = field(default_factory=dict)
    stop_reason: str = ""
    iterations: int = 0


def create_context(cfg: Config, profile_path: str | None = None,
                   run_id: str | None = None) -> AssessmentContext:
    run_id = run_id or time.strftime("run-%Y%m%d-%H%M%S")
    run_dir = new_run_dir(cfg, run_id)
    profile_path = profile_path or str(
        Path(__file__).resolve().parent.parent / "benchmarks" / "systems" / "online_boutique.yaml")
    profile = yaml.safe_load(Path(profile_path).read_text())
    contracts = load_contracts()
    slo = load_slo(cfg.resolve(cfg.slo_file))
    b = cfg.budget
    budget = Budget(max_experiments=b.get("max_experiments", 8),
                    max_wall_time_s=b.get("max_wall_time_s", 3600),
                    max_tokens=b.get("max_tokens", 200000),
                    lambda_cost=b.get("lambda_cost", 0.15),
                    mu_risk=b.get("mu_risk", 0.30))
    llm = LLMClient(cfg)
    graph = EvidenceGraph(cfg, run_id)
    tools = AgentTools(cfg, graph, profile)
    ctx = AssessmentContext(
        cfg=cfg, run_id=run_id, run_dir=run_dir, profile=profile, graph=graph,
        contracts=contracts, contracts_by_id={c.id: c for c in contracts},
        slo=slo, budget=budget, llm=llm, tools=tools,
        hypothesis_agent=HypothesisAgent(llm, tools),
        planner=ExperimentPlanner(llm, tools, cfg),
        proposer=ExperimentProposer(llm, tools, cfg),
        judge=ExperimentJudge(llm),
        analyst=ResultAnalyst(llm),
        safety=SafetyController(cfg))
    ctx.feedback = FeedbackUpdater(graph)
    return ctx


# ---------- 步骤 1-3：采集与建图 ----------
def step_collect(ctx: AssessmentContext) -> dict:
    cfg, profile = ctx.cfg, ctx.profile
    meta = {"started_at": time.time()}
    semgrep_ev = semgrep_adapter.collect(cfg, profile)
    ts_facts = tree_sitter_adapter.collect(cfg, profile)
    manifest = manifest_collector.collect(cfg, profile)
    k8s = k8s_collector.collect(cfg, profile)
    jaeger_res = jaeger_collector.collect(cfg, profile)
    prom_res = prometheus_collector.collect(cfg, profile, k8s.get("snapshot", {}))
    events = event_collector.collect(cfg, profile)
    extra = external_tools.run_kubelinter(cfg)
    extra += external_tools.run_conftest(
        cfg, Path(__file__).resolve().parent.parent / "analyzers" / "policy")

    runtime_ev = jaeger_res["evidence"] + prom_res["evidence"] + events["evidence"]
    alignment = ctx.graph.ingest(ts_facts, k8s, manifest, runtime_ev, profile, ctx.contracts)
    for ev in semgrep_ev + extra:
        ctx.graph.add_evidence(ev)

    extra_evidence, architecture_meta = architecture.collect(cfg, profile)
    for ev in extra_evidence:
        if ev.layer == "deployment" and not ev.subject.startswith(("namespace/", "container/")):
            ev.subject = ctx.graph.resolver.resolve_deployment(ev.subject) or ev.subject
        ctx.graph.add_evidence(ev)
    meta["architecture"] = architecture_meta

    meta.update({
        "source_evidence": len(semgrep_ev) + len(ts_facts["evidence"]),
        "functions": len(ts_facts["functions"]), "calls": len(ts_facts["calls"]),
        "desired_evidence": len(manifest["evidence"]),
        "observed_evidence": len(k8s["evidence"]),
        "runtime_evidence": len(runtime_ev),
        "jaeger_available": jaeger_res.get("available", False),
        "prometheus_available": prom_res.get("available", False),
        "architecture_metric_errors": prom_res.get("architecture_metric_errors", {}),
        "alignment": alignment,
        "graph_degraded": ctx.graph.degraded,
        "finished_at": time.time()})
    ctx.collection_meta = meta
    ctx.graph.save(ctx.run_dir / "evidence_graph.json")
    with open(ctx.run_dir / "collection_meta.json", "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
    return meta


# ---------- 步骤 4-5：确定性分析 + 假设 ----------
def step_deterministic(ctx: AssessmentContext) -> dict:
    ctx.graph_analysis = graph_analyzer.analyze(ctx.graph)
    direct, hypotheses = risk_generator.generate(ctx.graph, ctx.contracts, ctx.cfg.system_id)
    for c, svc, result in direct:
        state = result["state"]
        ctx.coverage[(c.id, svc)] = state
        if state == CS.NOT_APPLICABLE.value:
            continue
        f = Finding(
            contract_id=c.id, target=svc, state=state,
            category={CS.VIOLATED.value: "compliance_violation",
                      CS.COMPLIANT.value: "compliance"}.get(state, "unconfirmed_risk"),
            severity=c.severity, dimension=c.dimension,
            summary=f"{c.name}: {result['detail']}",
            evidence_ids=result.get("evidence_ids", []),
            root_cause_key=f"{c.id}|{svc}|{result['detail'][:120]}",
            system_id=ctx.cfg.system_id)
        ctx.findings.append(f)
        ctx.graph.add_finding(f)
    # Conftest 结构化策略证据（metadata.contract/target）-> 确定性合规发现
    n_conftest = 0
    for ev in ctx.graph.evidence(kind="conftest_policy"):
        meta = ev.get("attrs", {}).get("metadata") or {}
        cid, target = meta.get("contract"), meta.get("target")
        c = ctx.contracts_by_id.get(cid)
        if not c or not target or \
                ctx.coverage.get((cid, target)) == CS.VIOLATED.value:
            continue
        ctx.coverage[(cid, target)] = CS.VIOLATED.value
        # conftest 将 deny 对象的 msg 键提升为 failure.msg，metadata 仅余 contract/target
        msg = ev.get("attrs", {}).get("msg") or meta.get("msg", "")
        f = Finding(
            contract_id=cid, target=target, state=CS.VIOLATED.value,
            category="compliance_violation", severity=c.severity, dimension=c.dimension,
            summary=f"{c.name}: [conftest] {msg}",
            evidence_ids=[ev["evidence_id"]],
            root_cause_key=f"{cid}|{target}|conftest:{msg[:100]}",
            system_id=ctx.cfg.system_id)
        ctx.findings.append(f)
        ctx.graph.add_finding(f)
        n_conftest += 1
    for h in hypotheses:
        ctx.coverage.setdefault((h.contract_id, h.target), CS.RISK_UNCONFIRMED.value)
        ctx.graph.add_hypothesis(h)
    ctx.hypotheses = hypotheses
    return {"direct": len(direct), "conftest": n_conftest, "hypotheses": len(hypotheses),
            "graph_analysis": ctx.graph_analysis}


def step_hypothesis_agent(ctx: AssessmentContext):
    agent_contracts = {contract_id: contract for contract_id, contract in ctx.contracts_by_id.items()
                       if contract.actions}
    ctx.hypotheses = ctx.hypothesis_agent.run(ctx.hypotheses, agent_contracts)
    for h in ctx.hypotheses:
        if h.hypothesis_id not in ctx.graph.nodes:
            ctx.graph.add_hypothesis(h)   # agent_proposed 新假设：建完整关系边
        else:
            ctx.graph.update_hypothesis(h)
        ctx.coverage.setdefault((h.contract_id, h.target), CS.RISK_UNCONFIRMED.value)


# ---------- 步骤 6：候选池 -> 安全过滤 -> EGV 安全集 -> Agent 终裁 ----------
def _agent_enabled(ctx: AssessmentContext) -> bool:
    acfg = ctx.cfg.get("agent", {}) or {}
    return bool(acfg.get("enabled", True)) and ctx.llm.available()


def _dedup_candidates(candidates: list[dict]) -> list[dict]:
    """按 (假设,动作,故障对象,观测对象) 去重；静态候选优先保留。"""
    seen, out = set(), []
    for c in candidates:
        key = (c["hypothesis_id"], c["action_type"],
               c["fault_target"], c["observe_target"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def step_select(ctx: AssessmentContext) -> dict | None:
    unresolved = [h for h in ctx.hypotheses
                  if h.status in (CS.RISK_UNCONFIRMED.value, CS.INCONCLUSIVE.value)]
    if not unresolved:
        ctx.stop_reason = "all_resolved"
        return None
    if ctx.selector is None:
        ctx.selector = ExperimentSelector(ctx.graph, ctx.budget, ctx.feedback.history)
    elif hasattr(ctx.selector, "estimator"):
        ctx.selector.estimator.history = ctx.feedback.history
    hyps_by_id = {h.hypothesis_id: h for h in unresolved}
    candidates = action_generator.generate(unresolved, ctx.graph,
                                           ctx.contracts_by_id, ctx.cfg)
    if _agent_enabled(ctx):
        # Agent 提议通道：静态枚举盲区 + analyst followup 闭环
        candidates += ctx.proposer.propose(unresolved, candidates, ctx.contracts_by_id)
        for prop in ctx.pending_proposals:
            cand = action_generator.normalize_proposal(
                prop, hyps_by_id, ctx.contracts_by_id, ctx.cfg, ctx.graph)
            if cand is not None:
                cand["source"] = "analyst_followup"
                candidates.append(cand)
        ctx.pending_proposals = []
    candidates = _dedup_candidates(candidates)
    candidates = ctx.safety.filter_candidates(candidates, ctx.graph)
    if not candidates:
        ctx.stop_reason = "no_safe_candidates"
        return None
    # 非 Agent 模式或固定优先级选择器（机制消融）：保留纯算法 top-1 语义
    if not _agent_enabled(ctx) or not hasattr(ctx.selector, "safe_set"):
        best = ctx.selector.select(candidates, hyps_by_id)
        if best is None:
            ctx.stop_reason = stopping_policy.should_stop(ctx.budget, unresolved, 0.0) \
                or "no_positive_utility_action"
            return None
        return best
    if ctx.algo_enabled:
        acfg = ctx.cfg.get("agent", {}) or {}
        safe = ctx.selector.safe_set(candidates, hyps_by_id,
                                     k=int(acfg.get("safe_set_k", 3)))
        if not safe:
            ctx.stop_reason = stopping_policy.should_stop(ctx.budget, unresolved, 0.0) \
                or "no_positive_utility_action"
            return None
        fallback = "top_utility"
    else:
        # algo=False 消融：跳过 EGV 打分，安全过滤后的完整池交 Agent 直选
        safe = candidates
        fallback = "min_cost"
    best = ctx.judge.decide(safe, hyps_by_id, ctx.feedback.history,
                            ctx.run_dir, fallback=fallback)
    if best is None:
        ctx.stop_reason = "agent_declined_execution"
        return None
    return best


# ---------- 步骤 7-9：执行 + Oracle + 反馈 ----------
def step_execute(ctx: AssessmentContext, candidate: dict):
    h_by_id = {h.hypothesis_id: h for h in ctx.hypotheses}
    h = h_by_id[candidate["hypothesis_id"]]
    c = ctx.contracts_by_id[h.contract_id]
    x = ctx.planner.plan(candidate, h, c, ctx.slo)
    ctx.graph.add_experiment(x)
    ctx.selector.record_attempt(candidate)

    tokens_before = ctx.llm.tokens_used
    obs, verdict = controller.execute_controlled(
        ctx.cfg, ctx.graph, x, c, ctx.slo, ctx.safety, ctx.run_dir, llm=ctx.llm)

    # 共享实验：同一故障可判定多个兼容假设
    xdict = x.to_dict() | {"provides": candidate.get("provides", [])}
    for h2 in ctx.hypotheses:
        if h2.status not in (CS.RISK_UNCONFIRMED.value, CS.INCONCLUSIVE.value):
            continue
        if h2.hypothesis_id != h.hypothesis_id and not _compatible(candidate, h2, ctx.graph):
            continue
        ctx.feedback.update(h2, xdict, obs["during"], verdict)
        ctx.coverage[(h2.contract_id, h2.target)] = h2.status
        # 共享实验关联回写：使 finalize 能按实验 id 反查到观测（避免误降级）
        if x.experiment_id not in h2.experiment_ids:
            h2.experiment_ids.append(x.experiment_id)
        ctx.graph.update_hypothesis(h2)
        analysis = ctx.analyst.analyze(x, obs, verdict, h2)
        ctx.analyses.append(analysis)
        # followup 闭环：结构化跟进提议进入下一轮候选池（经门禁规整）
        fu = analysis.get("followup")
        if isinstance(fu, dict) and fu.get("action_type"):
            ctx.pending_proposals.append(fu | {"hypothesis_id": h2.hypothesis_id})

    ctx.budget.consume(x.cost_actual or candidate["estimated_cost"],
                       tokens=ctx.llm.tokens_used - tokens_before,
                       experiment=candidate["action_type"] not in READONLY_ACTIONS)
    ctx.iterations += 1
    with open(ctx.run_dir / "experiments.jsonl", "a") as f:
        f.write(json.dumps({"experiment": x.to_dict(), "verdict": verdict,
                            "candidate": candidate}, ensure_ascii=False, default=str) + "\n")
    return x, verdict


# ---------- 步骤 10：最终评估 ----------
def step_finalize(ctx: AssessmentContext) -> dict:
    for h in ctx.hypotheses:
        if h.status in (CS.VIOLATED.value, CS.COMPLIANT.value):
            category = "validated_defect" if h.status == CS.VIOLATED.value else "compliance"
        else:
            category = "unconfirmed_risk"
        exps = [e for e in ctx.graph.experiments()
                if e.get("hypothesis_id") == h.hypothesis_id
                or e.get("experiment_id") in getattr(h, "experiment_ids", [])]
        obs_ids = []
        for e in exps:
            obs_ids += [o["observation_id"] for o in ctx.graph.observations(e["experiment_id"])]
        c = ctx.contracts_by_id[h.contract_id]
        f = Finding(
            contract_id=h.contract_id, target=h.target, state=h.status,
            category=category, severity=c.severity, dimension=c.dimension,
            summary=h.statement, evidence_ids=h.evidence_ids,
            hypothesis_id=h.hypothesis_id,
            experiment_ids=[e["experiment_id"] for e in exps],
            observation_ids=obs_ids,
            root_cause_key=f"{h.contract_id}|{h.target}|{h.hypothesis_id}",
            system_id=ctx.cfg.system_id)
        ctx.findings.append(f)
        ctx.graph.add_finding(f)

    ctx.findings = finding_deduplication.deduplicate(ctx.findings)
    ctx.findings = evidence_validator.validate(ctx.findings, ctx.graph)
    report = report_generator.generate(ctx)
    ctx.graph.save(ctx.run_dir / "evidence_graph.json")
    with open(ctx.run_dir / "report.json", "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    (ctx.run_dir / "report.md").write_text(report_generator.to_markdown(report))
    return report


def run_loop(ctx: AssessmentContext) -> dict:
    """无 LangGraph 时的等价直接循环（降级路径）。"""
    step_collect(ctx)
    step_deterministic(ctx)
    step_hypothesis_agent(ctx)
    while ctx.budget.available() and ctx.iterations <= ctx.budget.max_experiments + 2:
        best = step_select(ctx)
        if best is None:
            break
        step_execute(ctx, best)
    if not ctx.stop_reason:
        ctx.stop_reason = "budget_exhausted"
    return step_finalize(ctx)
