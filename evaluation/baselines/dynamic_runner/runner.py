"""独立动态故障注入基线（方案 §9.3，ChaosEater/Filibuster 风格）。

可复现随机故障注入：固定种子随机选择 (动作, 故障对象, 观测对象)，
不使用 EGV 效用选择、假设生成与运行期反馈。复用与主系统相同的注入
适配器、观测窗口与 Oracle（保证测量口径一致），但选择策略完全独立。

契约映射（显式口径）：
  NETWORK_LOSS / NETWORK_DELAY / DEPENDENCY_UNAVAILABLE -> RES-005（依赖故障隔离）
  POD_TERMINATION                                       -> RES-004（Pod 故障恢复）

输出与主系统报告同构，并记录发现曲线（cumulative_findings vs 实验序号），
供 RQ3 效率指标与策略对比使用。
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from common.config import Config
from common.models import Experiment
from assessment import pipeline
from active_verification import action_generator
from active_verification.action_generator import ACTION_COST
from experiment_runtime import controller

# 动作 -> 判定契约
ACTION_CONTRACT = {
    "NETWORK_LOSS": "RES-005",
    "NETWORK_DELAY": "RES-005",
    "DEPENDENCY_UNAVAILABLE": "RES-005",
    "POD_TERMINATION": "RES-004",
}


def _pick(rng: random.Random, graph, profile: dict, cfg: Config,
          allowed: list[str]) -> tuple[str, str, str] | None:
    """随机选 (action, fault_target, observe_target)，跳过受保护/基础设施服务。"""
    protected = set(cfg.safety.get("protected_services", []))
    infra = set(profile.get("infra_services", []))
    services = [s for s in profile.get("critical_services", [])
                if s not in protected and s not in infra]
    if not services:
        return None
    actions = [a for a in allowed if a in ACTION_CONTRACT]
    if not actions:
        return None
    for _ in range(20):  # 重试上限，避免无下游时死循环
        action = rng.choice(actions)
        observe = rng.choice(services)
        if action == "POD_TERMINATION":
            if observe in protected:
                continue
            return action, observe, observe
        downs = [d for d in graph.downstream(observe)
                 if d not in protected and d not in infra
                 and not graph.service_props(d).get("infra")]
        fault = rng.choice(downs) if downs else observe
        if fault in protected:
            continue
        return action, fault, observe
    return None


def run_dynamic_baseline(cfg: Config, profile_path: str | None = None,
                         run_id: str | None = None, seed: int = 42,
                         n_experiments: int | None = None) -> dict:
    run_id = run_id or f"dynamic-baseline-{int(time.time())}"
    ctx = pipeline.create_context(cfg, profile_path, run_id)
    pipeline.step_collect(ctx)

    n = n_experiments or ctx.budget.max_experiments
    rng = random.Random(seed)
    allowed = cfg.safety.get("allowed_actions", [])
    profile = ctx.profile

    findings, compliant, inconclusive = [], [], []
    curve = []  # [{"index", "action", "fault_target", "observe_target", "state", "cumulative_defects"}]
    cumulative = 0
    t_start = time.time()

    for i in range(n):
        pick = _pick(rng, ctx.graph, profile, cfg, allowed)
        if pick is None:
            break
        action, fault, observe = pick
        cid = ACTION_CONTRACT[action]
        contract = ctx.contracts_by_id[cid]
        params = action_generator._params(action, None, ctx.graph, cfg)
        x = Experiment(
            hypothesis_id=f"dyn-{seed}-{i}", action_type=action, target=fault,
            fault_target=fault, observe_target=observe, params=params,
            estimated_cost=ACTION_COST.get(action, 180), run_id=run_id)
        ctx.graph.add_experiment(x)
        try:
            obs, verdict = controller.execute_controlled(
                cfg, ctx.graph, x, contract, ctx.slo, ctx.safety, ctx.run_dir)
        except Exception as e:  # 单实验失败不中断基线
            verdict = {"state": "INCONCLUSIVE", "reasons": [f"execution error: {e}"]}
            obs = {}
        state = verdict["state"]
        rec = {"index": i, "experiment_id": x.experiment_id, "action": action,
               "fault_target": fault, "observe_target": observe,
               "contract_id": cid, "state": state,
               "cost_s": round(x.cost_actual or 0, 1),
               "reasons": verdict.get("reasons", [])}
        if state == "VIOLATED":
            cumulative += 1
            findings.append({"contract_id": cid, "target": observe, "state": state,
                             "severity": contract.severity, "source_tool": "dynamic_baseline",
                             "message": f"{action} on {fault}: " + "; ".join(verdict.get("reasons", []))})
        elif state == "COMPLIANT":
            compliant.append({"contract_id": cid, "target": observe, "state": state,
                              "severity": contract.severity, "source_tool": "dynamic_baseline",
                              "message": f"{action} on {fault}: SLO held"})
        else:
            inconclusive.append(rec)
        rec["cumulative_defects"] = cumulative
        curve.append(rec)
        with open(ctx.run_dir / "experiments.jsonl", "a") as f:
            f.write(json.dumps({"experiment": x.to_dict(), "verdict": verdict},
                               ensure_ascii=False, default=str) + "\n")

    # 去重 (contract, target)
    def _dedup(items):
        seen, out = set(), []
        for f in items:
            k = (f["contract_id"], f["target"])
            if k not in seen:
                seen.add(k)
                out.append(f)
        return out

    services = [s for s in profile.get("critical_services", [])
                if s not in set(cfg.safety.get("protected_services", []))]
    baseline_supported = sorted(
        [(c, s) for c in ("RES-004", "RES-005") for s in services])

    report = {
        "run_id": run_id,
        "method": "dynamic_baseline",
        "system_id": profile.get("system_id", "unknown"),
        "seed": seed,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(time.time() - t_start, 1),
        "n_experiments": len(curve),
        "compliance_findings": [],
        "validated_runtime_findings": _dedup(findings),
        "compliant": _dedup(compliant),
        "inconclusive": inconclusive,
        "discovery_curve": curve,
        "baseline_supported": baseline_supported,
    }
    (ctx.run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str))
    ctx.graph.save(ctx.run_dir / "evidence_graph.json")
    report["_path"] = str(ctx.run_dir / "report.json")
    return report