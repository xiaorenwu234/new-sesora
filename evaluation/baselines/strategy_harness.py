"""策略对比实验 harness（方案 §9.3 / RQ3）。

与主系统共享候选池、安全过滤、注入适配器、观测窗口与 Oracle，
仅替换实验选择策略，保证效率对比的公平性：
  egv            —— 本方法（ExperimentSelector，默认）
  random         —— 固定种子随机
  centrality     —— 依赖图介数中心性启发式
  fixed_priority —— 契约权重 + 动作侵入性固定规则
  llm_only       —— LLM 直接排序（无显式效用分解）

每个策略产出独立 run_dir（report.json + experiments.jsonl），
由 efficiency_metrics.compare_strategies 汇总发现曲线/AUC/单位成本。
"""
from __future__ import annotations

from common.config import Config
from assessment import pipeline
from evaluation.baselines.strategy_baselines import STRATEGIES

ALL_STRATEGIES = ("egv", "random", "centrality", "fixed_priority", "llm_only")


def _make_selector(strategy: str, ctx):
    if strategy == "egv":
        return None  # pipeline 默认构造 ExperimentSelector
    cls = STRATEGIES[strategy]
    kw = {}
    if strategy == "random":
        kw["seed"] = 42
    if strategy == "llm_only":
        kw["llm"] = ctx.llm
    return cls(ctx.graph, ctx.budget, ctx.feedback.history, **kw)


def run_strategy(cfg: Config, strategy: str, profile_path: str | None = None,
                 run_id: str | None = None, n_experiments: int | None = None) -> dict:
    if strategy not in ALL_STRATEGIES:
        raise ValueError(f"unknown strategy {strategy}; choose from {ALL_STRATEGIES}")
    run_id = run_id or f"strategy-{strategy}"
    ctx = pipeline.create_context(cfg, profile_path, run_id)
    if n_experiments:
        ctx.budget.max_experiments = n_experiments

    pipeline.step_collect(ctx)
    pipeline.step_deterministic(ctx)
    pipeline.step_hypothesis_agent(ctx)
    # selector 在建图后构造（centrality 需要依赖图）
    ctx.selector = _make_selector(strategy, ctx)

    while ctx.budget.available() and ctx.iterations <= ctx.budget.max_experiments + 2:
        best = pipeline.step_select(ctx)
        if best is None:
            break
        pipeline.step_execute(ctx, best)
    if not ctx.stop_reason:
        ctx.stop_reason = "budget_exhausted"
    report = pipeline.step_finalize(ctx)
    report["strategy"] = strategy
    return report


def run_all(cfg: Config, profile_path: str | None = None,
            n_experiments: int | None = None,
            strategies: tuple = ALL_STRATEGIES) -> dict[str, dict]:
    reports = {}
    for s in strategies:
        reports[s] = run_strategy(cfg, s, profile_path,
                                  run_id=f"strategy-{s}", n_experiments=n_experiments)
    return reports