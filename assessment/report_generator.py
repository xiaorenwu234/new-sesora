"""评估报告生成（方案 §七 报告结构）。

成熟度等级是派生结果：第一版只输出维度级违例数/覆盖率/未知项/严重度。
"""
from __future__ import annotations

import time
from collections import defaultdict

from evidence_graph.provenance import evidence_chain


def generate(ctx) -> dict:
    findings = [f.to_dict() for f in ctx.findings]
    by_dim = defaultdict(lambda: {"violations": 0, "validated_defects": 0,
                                  "compliant": 0, "unconfirmed": 0, "inconclusive": 0,
                                  "contracts_evaluated": set(), "critical_high": 0})
    for f in findings:
        d = by_dim[f["dimension"] or "unknown"]
        d["contracts_evaluated"].add(f["contract_id"])
        if f["category"] == "compliance_violation":
            d["violations"] += 1
        elif f["category"] == "validated_defect":
            d["validated_defects"] += 1
        elif f["state"] == "COMPLIANT":
            d["compliant"] += 1
        elif f["state"] == "INCONCLUSIVE":
            d["inconclusive"] += 1
        else:
            d["unconfirmed"] += 1
        if f["severity"] in ("critical", "high") and f["state"] == "VIOLATED":
            d["critical_high"] += 1
    dimensions = {}
    for dim, d in by_dim.items():
        dimensions[dim] = {k: (sorted(v) if isinstance(v, set) else v)
                           for k, v in d.items()}

    applicable = len(ctx.coverage)
    evaluated = sum(1 for s in ctx.coverage.values()
                    if s in ("COMPLIANT", "VIOLATED", "NOT_APPLICABLE"))
    chains = []
    for f in findings:
        if f["state"] == "VIOLATED":
            chains.append({"finding_id": f["finding_id"],
                           "contract": f["contract_id"], "target": f["target"],
                           "chain": evidence_chain(ctx.graph, f)})

    return {
        "system_metadata": {
            "system_id": ctx.cfg.system_id, "run_id": ctx.run_id,
            "namespace": ctx.cfg.kubernetes.namespace,
            "source_commit": ctx.collection_meta.get("alignment", {}) and
            ctx.cfg.source.get("commit") or "see collection_meta",
            "generated_at": time.time(),
            "observation_window": ctx.cfg.runtime.get("lookback"),
            "llm_enabled": ctx.llm.available(),
            "graph_degraded": ctx.graph.degraded},
        "coverage_summary": {
            "applicable_contract_service_pairs": applicable,
            "evaluated_pairs": evaluated,
            "inconclusive_pairs": sum(1 for s in ctx.coverage.values()
                                      if s == "INCONCLUSIVE"),
            "unconfirmed_pairs": sum(1 for s in ctx.coverage.values()
                                     if s == "RISK_UNCONFIRMED"),
            "coverage_ratio": round(evaluated / applicable, 3) if applicable else 0.0},
        "dimension_summary": dimensions,
        "compliance_findings": [f for f in findings
                                if f["category"] == "compliance_violation"],
        "validated_runtime_findings": [f for f in findings
                                       if f["category"] == "validated_defect"],
        "unconfirmed_risks": [f for f in findings
                              if f["category"] == "unconfirmed_risk"],
        "compliant": [f for f in findings if f["state"] == "COMPLIANT"],
        "cross_layer_evidence_chains": chains,
        "experiment_records": ctx.graph.experiments(),
        "experiment_analyses": ctx.analyses,
        "selection_ranking_log": ctx.selector.ranking_log if ctx.selector else [],
        "safety_decisions": ctx.safety.decisions,
        "entity_alignment": ctx.collection_meta.get("alignment", {}),
        "graph_analysis": ctx.graph_analysis,
        "budget": ctx.budget.to_dict(),
        "stop_reason": ctx.stop_reason,
        "collection_meta": ctx.collection_meta,
        "reproduction_artifacts": {
            "run_dir": str(ctx.run_dir),
            "evidence_graph": "evidence_graph.json",
            "experiments": "experiments.jsonl",
            "config": ctx.cfg.get("_config_path", "config.yaml")},
    }


def to_markdown(report: dict) -> str:
    L = []
    meta = report["system_metadata"]
    L.append(f"# 云原生架构成熟度评估报告 — {meta['system_id']}")
    L.append(f"\n- Run: `{meta['run_id']}`  命名空间: `{meta['namespace']}`")
    L.append(f"- LLM Agent: {'启用' if meta['llm_enabled'] else '确定性回退模式'}"
             f"  图谱: {'Neo4j 降级(内存)' if meta['graph_degraded'] else 'Neo4j'}")
    for source, status in report.get("collection_meta", {}).get("architecture", {}).items():
        if status.get("error"):
            L.append(f"- {source} 采集失败：{status['error']}")
    cov = report["coverage_summary"]
    L.append("\n## 覆盖率摘要\n")
    L.append(f"| 适用契约-服务对 | 已判定 | 待验证 | 不确定 | 覆盖率 |")
    L.append(f"|---|---|---|---|---|")
    L.append(f"| {cov['applicable_contract_service_pairs']} | {cov['evaluated_pairs']} "
             f"| {cov['unconfirmed_pairs']} | {cov['inconclusive_pairs']} "
             f"| {cov['coverage_ratio']:.1%} |")
    L.append("\n## 维度摘要\n")
    L.append("| 维度 | 规范违规 | 已验证缺陷 | 合规 | 未确认 | 不确定 | 高危违例 |")
    L.append("|---|---|---|---|---|---|---|")
    for dim, d in report["dimension_summary"].items():
        L.append(f"| {dim} | {d['violations']} | {d['validated_defects']} | {d['compliant']} "
                 f"| {d['unconfirmed']} | {d['inconclusive']} | {d['critical_high']} |")

    def section(title, items):
        L.append(f"\n## {title}（{len(items)}）\n")
        for f in items:
            L.append(f"### [{f['contract_id']}] {f['target']} — {f['state']}")
            L.append(f"- 严重度: {f['severity']}  类别: {f['category']}")
            L.append(f"- {f['summary']}")
            if f.get("experiment_ids"):
                L.append(f"- 实验: {', '.join(f['experiment_ids'])}")
            if f.get("evidence_ids"):
                L.append(f"- 证据数: {len(f['evidence_ids'])}")

    section("规范违规（确定性判定）", report["compliance_findings"])
    section("已验证运行缺陷（动态实验证实）", report["validated_runtime_findings"])
    section("未确认风险", report["unconfirmed_risks"])

    L.append(f"\n## 实验记录（{len(report['experiment_records'])}）\n")
    L.append("| 实验 | 动作 | 故障对象 | 观测对象 | 状态 | 效用 | 耗时(s) |")
    L.append("|---|---|---|---|---|---|---|")
    for x in report["experiment_records"]:
        L.append(f"| {x.get('experiment_id')} | {x.get('action_type')} "
                 f"| {x.get('fault_target') or x.get('target')} | {x.get('observe_target')} "
                 f"| {x.get('status')} | {x.get('utility', 0):.3f} "
                 f"| {x.get('cost_actual', 0):.0f} |")
    for a in report.get("experiment_analyses", []):
        L.append(f"\n**{a['experiment_id']}**: {a['explanation']}")
    L.append(f"\n## 停止原因: `{report['stop_reason']}`")
    b = report["budget"]
    L.append(f"\n预算使用: 实验 {b['used_experiments']}/{b['max_experiments']}, "
             f"时间 {b['used_wall_time_s']:.0f}s/{b['max_wall_time_s']}s, "
             f"tokens {b['used_tokens']}/{b['max_tokens']}")
    L.append(f"\n复现产物目录: `{report['reproduction_artifacts']['run_dir']}`")
    return "\n".join(L) + "\n"
