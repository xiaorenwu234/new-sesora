"""CLI 入口：collect / analyze / assess / experiment / evaluate / mutation / cleanup。

用法示例（sesora conda 环境）：
    python cli.py assess --static-only          # 静态评估（不注入故障）
    python cli.py assess --max-experiments 3    # 完整主动评估闭环
    python cli.py experiment --contract RES-001 --target frontend --action NETWORK_DELAY
    python cli.py evaluate metrics --ours runs/full-e2e-06 --baseline runs/static-baseline-test
    python cli.py evaluate strategies --runs egv=runs/a random=runs/b --budget 8
    python cli.py evaluate baseline --type dynamic --n 3 --seed 42
    python cli.py evaluate ablation --variant no_cost
    python cli.py mutation apply MUT-001 --deploy
    python cli.py cleanup                       # 紧急清理所有 cnaa- 故障 CR
"""
from __future__ import annotations

import argparse
import json
import sys

from common.config import Config


def _ctx(args):
    from assessment.pipeline import create_context
    cfg = Config.load(args.config)
    if getattr(args, "max_experiments", None) is not None:
        cfg["budget"]["max_experiments"] = args.max_experiments
    return create_context(cfg, profile_path=args.profile, run_id=args.run_id)


def cmd_collect(args):
    from assessment import pipeline
    ctx = _ctx(args)
    meta = pipeline.step_collect(ctx)
    print(json.dumps(meta, ensure_ascii=False, indent=2, default=str))
    print(f"\n产物目录: {ctx.run_dir}")
    return 0


def cmd_analyze(args):
    from assessment import pipeline
    ctx = _ctx(args)
    pipeline.step_collect(ctx)
    stats = pipeline.step_deterministic(ctx)
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    print(f"\n产物目录: {ctx.run_dir}")
    return 0


def _run_assessment(ctx, static_only: bool, use_workflow: bool):
    from assessment import pipeline
    if use_workflow and not static_only:
        from agents import workflow
        return workflow.run(ctx)
    pipeline.step_collect(ctx)
    pipeline.step_deterministic(ctx)
    pipeline.step_hypothesis_agent(ctx)
    if static_only:
        ctx.stop_reason = "static_only"
        return pipeline.step_finalize(ctx)
    while ctx.budget.available() and ctx.iterations <= ctx.budget.max_experiments + 2:
        best = pipeline.step_select(ctx)
        if best is None:
            break
        print(f"[experiment {ctx.iterations + 1}] {best['action_type']} "
              f"fault={best['fault_target']} observe={best['observe_target']} "
              f"utility={best.get('utility', 0):.3f}", flush=True)
        pipeline.step_execute(ctx, best)
    if not ctx.stop_reason:
        ctx.stop_reason = "budget_exhausted"
    return pipeline.step_finalize(ctx)


def cmd_assess(args):
    ctx = _ctx(args)
    report = _run_assessment(ctx, args.static_only, args.workflow)
    cov = report["coverage_summary"]
    print(f"\n覆盖率: {cov['evaluated_pairs']}/{cov['applicable_contract_service_pairs']} "
          f"({cov['coverage_ratio']:.1%})  停止原因: {report['stop_reason']}")
    for dim, d in report["dimension_summary"].items():
        print(f"  {dim}: 违规={d['violations']} 已验证缺陷={d['validated_defects']} "
              f"合规={d['compliant']} 未确认={d['unconfirmed']}")
    print(f"\n报告: {ctx.run_dir / 'report.md'}")
    return 0


def cmd_experiment(args):
    """手动执行单个受控实验（复用同一条执行链路，用于调试/复现）。"""
    from assessment import pipeline
    from common.models import Hypothesis
    ctx = _ctx(args)
    pipeline.step_collect(ctx)
    pipeline.step_deterministic(ctx)
    c = ctx.contracts_by_id.get(args.contract)
    if c is None:
        print(f"未知契约: {args.contract}", file=sys.stderr)
        return 1
    if not c.actions:
        print(f"{c.id} 由证据直接判定，请使用 assess", file=sys.stderr)
        return 1
    h = next((x for x in ctx.hypotheses
              if x.contract_id == args.contract and x.target == args.target), None)
    if h is None:
        h = Hypothesis(contract_id=c.id, target=args.target,
                       statement=f"manual verification of {c.id} on {args.target}",
                       missing_evidence=[], system_id=ctx.cfg.system_id)
        ctx.hypotheses.append(h)
        ctx.graph.add_hypothesis(h)
    from active_verification import action_generator
    provides = action_generator.ACTION_PROVIDES.get(args.action, set())
    candidate = {
        "hypothesis_id": h.hypothesis_id, "contract_id": c.id, "action_type": args.action,
        "fault_target": args.fault_target or args.target,
        "observe_target": args.target,
        "params": json.loads(args.params) if args.params else {},
        "expected_evidence": sorted(provides & set(c.missing_evidence)),
        "provides": sorted(provides),
        "estimated_cost": 200, "estimated_risk": 0.3, "utility": 1.0}
    if not candidate["params"]:
        candidate["params"] = action_generator._params(args.action, h, ctx.graph, ctx.cfg)
    safe, reason = ctx.safety.validate_candidate(candidate, ctx.graph)
    if not safe:
        print(f"安全过滤器拒绝: {reason}", file=sys.stderr)
        return 1
    if ctx.selector is None:
        from active_verification.experiment_selector import ExperimentSelector
        ctx.selector = ExperimentSelector(ctx.graph, ctx.budget, ctx.feedback.history)
    x, verdict = pipeline.step_execute(ctx, candidate)
    ctx.graph.save(ctx.run_dir / "evidence_graph.json")
    print(json.dumps({"experiment": x.to_dict(), "verdict": verdict},
                     ensure_ascii=False, indent=2, default=str))
    print(f"\n产物目录: {ctx.run_dir}")
    return 0


def cmd_cleanup(args):
    from experiment_runtime.chaosblade_adapter import ChaosBladeAdapter
    cfg = Config.load(args.config)
    removed = ChaosBladeAdapter(cfg).cleanup_all()
    print(f"已清理 {len(removed)} 个实验 CR: {removed}")
    return 0


def _load_report(path):
    p = path if path.endswith(".json") else f"{path}/report.json"
    import os
    if not os.path.exists(p):
        p = f"{path}/static_baseline_report.json"
    with open(p) as f:
        return json.load(f)


def cmd_evaluate(args):
    cfg = Config.load(args.config)
    action = args.eval_action
    if action == "metrics":
        from evaluation.metrics.detection_metrics import load_ground_truth, evaluate, compare
        gt = load_ground_truth(args.gt)
        ours = _load_report(args.ours)
        result = {"ours": evaluate(ours, gt)}
        if args.baseline:
            base = _load_report(args.baseline)
            sup = {tuple(x) for x in base.get("baseline_supported", [])} or None
            result["comparison"] = compare(ours, base, gt, baseline_supported=sup)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif action == "efficiency":
        from evaluation.metrics.efficiency_metrics import load_records, efficiency_metrics
        recs = load_records(args.ours)
        print(json.dumps(efficiency_metrics(recs, args.budget), ensure_ascii=False, indent=2))
    elif action == "strategies":
        from evaluation.metrics.efficiency_metrics import compare_strategies
        runs = dict(kv.split("=", 1) for kv in args.runs)
        print(json.dumps(compare_strategies(runs, args.budget), ensure_ascii=False, indent=2))
    elif action == "baseline":
        if args.type == "static":
            from evaluation.baselines.static_runner.runner import run_static_baseline
            r = run_static_baseline(cfg, profile_path=args.profile, run_id=args.run_id)
            print(f"静态基线: {len(r['compliance_findings'])} findings -> {r['_path']}")
        else:
            from evaluation.baselines.dynamic_runner.runner import run_dynamic_baseline
            r = run_dynamic_baseline(cfg, profile_path=args.profile, run_id=args.run_id,
                                     seed=args.seed, n_experiments=args.n)
            print(f"动态基线: n={r['n_experiments']} "
                  f"defects={len(r['validated_runtime_findings'])} "
                  f"inconclusive={len(r['inconclusive'])} -> {r['_path']}")
    elif action == "ablation":
        from evaluation.ablations.ablation_runner import run_variant
        r = run_variant(cfg, args.variant, run_id=args.run_id, profile_path=args.profile)
        print(f"消融 {args.variant}: 停止={r['stop_reason']} "
              f"组件={r['components']}")
    elif action == "run-strategy":
        from evaluation.baselines.strategy_harness import run_strategy
        r = run_strategy(cfg, args.strategy, profile_path=args.profile,
                         run_id=args.run_id, n_experiments=args.n)
        print(f"策略 {args.strategy}: 停止={r['stop_reason']}")
    return 0


def cmd_mutation(args):
    from benchmarks.mutations import runner as mut
    cfg = Config.load(args.config)
    if args.mut_action == "list":
        for c in mut.load_cases():
            print(f"{c['id']:8} {c['contract_id']:8} {c['target']:24} "
                  f"{c['expected_state']:10} {c['description']}")
    elif args.mut_action == "apply":
        print(json.dumps(mut.apply_mutation(cfg, args.case, deploy=args.deploy),
                         ensure_ascii=False, indent=2))
    elif args.mut_action == "revert":
        print(json.dumps(mut.revert_mutation(cfg, args.case, deploy=args.deploy),
                         ensure_ascii=False, indent=2))
    elif args.mut_action == "revert-all":
        print(json.dumps(mut.revert_all(cfg, deploy=args.deploy), ensure_ascii=False, indent=2))
    elif args.mut_action == "status":
        print("已注入:", mut.applied_cases(cfg))
    return 0


def main():
    p = argparse.ArgumentParser(description="云原生架构成熟度主动评估系统")
    p.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--profile", default=None, help="系统画像 YAML")
        sp.add_argument("--run-id", default=None)
        sp.add_argument("--max-experiments", type=int, default=None)

    common(sub.add_parser("collect", help="仅采集证据并建图"))
    common(sub.add_parser("analyze", help="采集 + 确定性分析（不生成报告）"))
    sp = sub.add_parser("assess", help="端到端评估")
    common(sp)
    sp.add_argument("--static-only", action="store_true", help="跳过动态实验")
    sp.add_argument("--workflow", action="store_true", help="使用 LangGraph 编排")
    sp = sub.add_parser("experiment", help="手动执行单个受控实验")
    common(sp)
    sp.add_argument("--contract", required=True)
    sp.add_argument("--target", required=True, help="观测对象服务")
    sp.add_argument("--action", required=True,
                    choices=["NETWORK_DELAY", "NETWORK_LOSS", "DEPENDENCY_UNAVAILABLE",
                             "POD_TERMINATION", "CPU_PRESSURE", "MEMORY_PRESSURE",
                             "JVM_DELAY", "JVM_EXCEPTION",
                             "LOAD_VARIATION", "CONTROLLED_REDEPLOYMENT", "OBSERVATION_ONLY",
                             "LOG_DEEP_DIVE", "TRACE_DRILLDOWN", "METRIC_DRILLDOWN",
                             "CONFIG_AUDIT", "SOURCE_INSPECTION"])
    sp.add_argument("--fault-target", default=None, help="故障施加对象（默认同 target）")
    sp.add_argument("--params", default=None, help="JSON 参数覆盖")
    sub.add_parser("cleanup", help="紧急清理所有 cnaa- 故障 CR")

    sp = sub.add_parser("evaluate", help="评估指标/基线/消融/策略对比")
    sp.add_argument("--profile", default=None)
    sp.add_argument("--run-id", default=None)
    esp = sp.add_subparsers(dest="eval_action", required=True)
    m = esp.add_parser("metrics", help="检测指标 P/R/F1 + Comparison A/B")
    m.add_argument("--ours", required=True, help="主系统 run_dir 或 report.json")
    m.add_argument("--baseline", default=None, help="基线 run_dir")
    m.add_argument("--gt", default="benchmarks/ground_truth/boutique_ground_truth.yaml")
    e = esp.add_parser("efficiency", help="RQ3 效率指标（单 run）")
    e.add_argument("--ours", required=True)
    e.add_argument("--budget", type=int, default=None)
    s = esp.add_parser("strategies", help="RQ3 多策略效率对比")
    s.add_argument("--runs", nargs="+", required=True, help="name=run_dir ...")
    s.add_argument("--budget", type=int, default=None)
    b = esp.add_parser("baseline", help="运行独立基线")
    b.add_argument("--type", choices=["static", "dynamic"], required=True)
    b.add_argument("--seed", type=int, default=42)
    b.add_argument("--n", type=int, default=None)
    a = esp.add_parser("ablation", help="运行消融变体")
    a.add_argument("--variant", required=True)
    rs = esp.add_parser("run-strategy", help="运行单个策略对比实验")
    rs.add_argument("--strategy", required=True)
    rs.add_argument("--n", type=int, default=None)

    mp = sub.add_parser("mutation", help="受控缺陷注入 apply/revert")
    msp = mp.add_subparsers(dest="mut_action", required=True)
    msp.add_parser("list", help="列出所有案例")
    ma = msp.add_parser("apply", help="注入缺陷")
    ma.add_argument("case")
    ma.add_argument("--deploy", action="store_true", help="kubectl apply 到集群")
    mr = msp.add_parser("revert", help="还原缺陷")
    mr.add_argument("case")
    mr.add_argument("--deploy", action="store_true")
    mra = msp.add_parser("revert-all", help="还原所有已注入案例")
    mra.add_argument("--deploy", action="store_true")
    msp.add_parser("status", help="查看已注入案例")

    args = p.parse_args()
    fn = {"collect": cmd_collect, "analyze": cmd_analyze, "assess": cmd_assess,
          "experiment": cmd_experiment, "cleanup": cmd_cleanup,
          "evaluate": cmd_evaluate, "mutation": cmd_mutation}[args.cmd]
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
