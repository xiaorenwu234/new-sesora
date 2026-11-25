"""消融实验（方案 §9.3 / RQ4）。

机制消融：no_kg / no_active_selection / no_runtime_feedback / no_cost /
no_inconclusive。组件矩阵（算法 × Agent × 动态验证）：
  v1_algorithm_only  算法 only
  v2_agent_only      Agent only（自主假设+Agent 直选候选，无确定性判定/无动态）
  v3_algo_agent      算法 + Agent（无动态验证）
  v4_algo_dynamic    算法 + 动态（无 Agent，LLM 关闭走规则回退）
  v5_agent_dynamic   Agent + 动态（无确定性算法判定，Agent 驱动选择与执行）
  full               全组件

各版本均提供合理替代实现（§10 RQ4）：移除组件后系统仍可运行——
无算法（algo=False）时跳过确定性判定与 EGV 打分，安全过滤后的完整
候选池直接交 Agent Judge 终裁（V2/V5 真正由 Agent 驱动选择），Judge
不可用时回退池内最低成本候选；无 Agent 时 propose/judge/followup 全
回退，planner/analyst 走规则模板，选择器纯 EGV top-1；无动态时假设
保留 RISK_UNCONFIRMED 而非强行判定。
no_inconclusive 将证据不足的 INCONCLUSIVE 强制转为 COMPLIANT（乐观
二值化），用于度量保留 INCONCLUSIVE 状态对避免错误合规的价值。
"""
from __future__ import annotations

from common.models import Finding, ContractState as CS
from assessment import pipeline
from evaluation.baselines.strategy_baselines import FixedPrioritySelector

VARIANTS = ("full", "no_kg", "no_active_selection", "no_runtime_feedback",
            "no_cost", "no_inconclusive",
            "v1_algorithm_only", "v2_agent_only", "v3_algo_agent",
            "v4_algo_dynamic", "v5_agent_dynamic")

# 组件矩阵开关：(算法, Agent, 动态验证)
_MATRIX = {
    "v1_algorithm_only": (True, False, False),
    "v2_agent_only": (False, True, False),
    "v3_algo_agent": (True, True, False),
    "v4_algo_dynamic": (True, False, True),
    "v5_agent_dynamic": (False, True, True),
}


def _components(variant: str) -> tuple[bool, bool, bool]:
    if variant in _MATRIX:
        return _MATRIX[variant]
    return True, True, True  # full 与机制消融均保留三组件


def _hypotheses_only(ctx):
    """无算法变体：保留假设生成（Agent 输入），跳过确定性合规判定。"""
    import analyzers.risk_generator as rg
    _, hypotheses = rg.generate(ctx.graph, ctx.contracts, ctx.cfg.system_id)
    ctx.hypotheses = hypotheses
    for h in hypotheses:
        ctx.coverage.setdefault((h.contract_id, h.target), CS.RISK_UNCONFIRMED.value)
        ctx.graph.add_hypothesis(h)


def _no_kg_deterministic(ctx):
    """no_kg：确定性契约检查保留，关闭图算法（依赖环检测）。"""
    import analyzers.risk_generator as rg
    ctx.graph_analysis = {"disabled": "no_kg ablation"}
    kg_contracts = [c for c in ctx.contracts
                    if c.static_check != "no_dependency_cycles"]
    direct, hypotheses = rg.generate(ctx.graph, kg_contracts, ctx.cfg.system_id)
    for c, svc, result in direct:
        ctx.coverage[(c.id, svc)] = result["state"]
        if result["state"] == CS.NOT_APPLICABLE.value:
            continue
        ctx.findings.append(Finding(
            contract_id=c.id, target=svc, state=result["state"],
            category="compliance_violation" if result["state"] == CS.VIOLATED.value
            else "compliance",
            severity=c.severity, dimension=c.dimension,
            summary=f"{c.name}: {result['detail']}",
            evidence_ids=result.get("evidence_ids", []),
            root_cause_key=f"{c.id}|{svc}|{result['detail'][:120]}",
            system_id=ctx.cfg.system_id))
    ctx.hypotheses = hypotheses
    for h in hypotheses:
        ctx.graph.add_hypothesis(h)


def run_variant(cfg, variant: str, run_id: str | None = None,
                profile_path: str | None = None) -> dict:
    assert variant in VARIANTS, f"unknown variant {variant}"
    algo, agent, dynamic = _components(variant)
    ctx = pipeline.create_context(cfg, profile_path=profile_path,
                                  run_id=run_id or f"ablation-{variant}")
    ctx.algo_enabled = algo  # algo=False：跳过 EGV，安全过滤池交 Agent 直选

    if variant == "no_cost":
        ctx.budget.lambda_cost = 0.0  # EGV 效用去除成本项

    # no_inconclusive：包裹 oracle，证据不足时强制乐观二值化
    from experiment_runtime import oracle_runner
    orig_oracle = oracle_runner.run_oracle
    if variant == "no_inconclusive":
        def forced_oracle(*a, **kw):
            v = orig_oracle(*a, **kw)
            if v["state"] == CS.INCONCLUSIVE.value:
                v = v | {"state": CS.COMPLIANT.value, "forced_binary": True,
                         "reasons": v["reasons"] + ["no_inconclusive: forced COMPLIANT"]}
            return v
        oracle_runner.run_oracle = forced_oracle

    if not agent:
        ctx.llm._chat = None  # 关闭 LLM，planner/analyst 走规则回退

    try:
        pipeline.step_collect(ctx)
        if variant == "no_kg":
            _no_kg_deterministic(ctx)
        elif algo:
            pipeline.step_deterministic(ctx)
        else:
            _hypotheses_only(ctx)
        if agent:
            pipeline.step_hypothesis_agent(ctx)

        if variant in ("no_kg", "no_active_selection"):
            ctx.selector = FixedPrioritySelector(ctx.graph, ctx.budget)

        if dynamic:
            while ctx.budget.available() and ctx.iterations <= ctx.budget.max_experiments + 2:
                best = pipeline.step_select(ctx)
                if best is None:
                    break
                pipeline.step_execute(ctx, best)
                if variant == "no_runtime_feedback":
                    ctx.feedback.history.clear()
        else:
            ctx.stop_reason = "dynamic_verification_disabled"
        if not ctx.stop_reason:
            ctx.stop_reason = "budget_exhausted"
        report = pipeline.step_finalize(ctx)
    finally:
        oracle_runner.run_oracle = orig_oracle

    report["ablation_variant"] = variant
    report["components"] = {"algorithm": algo, "agent": agent, "dynamic": dynamic}
    return report