"""候选验证动作生成器（EGV 输入 A 集合）。

动作空间是预定义枚举（方案 §4.5），每个候选动作绑定：
假设、故障对象、观测对象、参数、预期补充的证据类型、成本与风险估计。
除静态枚举 generate() 外，normalize_proposal() 提供 Agent 提议通道：
提议经同一动作空间/参数边界/受保护服务门禁规整后与静态候选同构。
"""
from __future__ import annotations

from common.models import ActionType as AT, READONLY_ACTIONS

# 动作类型 -> 可提供的证据类型（证据缺口覆盖计算的基础）
ACTION_PROVIDES = {
    AT.NETWORK_DELAY.value: {
        "upstream_slo_under_dependency_fault", "upstream_slo_under_delay",
        "runtime_timeout_behavior", "runtime_error_propagation",
        "fallback_behavior", "runtime_retry_behavior",
        "error_signal_during_fault", "error_span_attribution_during_fault",
        "duplicate_write_under_replay", "business_invariant_result",
        "downstream_load_amplification",
    },
    AT.NETWORK_LOSS.value: {
        "upstream_slo_under_dependency_fault", "runtime_timeout_behavior",
        "runtime_error_propagation", "fallback_behavior", "runtime_retry_behavior",
        "error_signal_during_fault", "error_span_attribution_during_fault",
        "duplicate_write_under_replay", "business_invariant_result",
        "invariant_state_after_partial_failure", "downstream_load_amplification",
    },
    AT.DEPENDENCY_UNAVAILABLE.value: {
        "upstream_slo_under_dependency_fault", "runtime_error_propagation",
        "fallback_behavior", "pod_restart_under_fault",
        "error_signal_during_fault", "error_span_attribution_during_fault",
        "invariant_state_after_partial_failure", "business_invariant_result",
    },
    AT.POD_TERMINATION.value: {
        "availability_during_pod_loss", "recovery_time",
        "request_loss_during_termination", "pod_restart_under_fault",
        "cross_replica_consistency_under_load", "invariant_state_after_partial_failure",
    },
    AT.CPU_PRESSURE.value: {"resource_usage_under_load", "upstream_slo_under_delay"},
    AT.MEMORY_PRESSURE.value: {"resource_usage_under_load", "pod_restart_under_fault"},
    AT.LOAD_VARIATION.value: {
        "throughput_under_load_increase", "resource_usage_under_load",
        "throughput_before_after_scale_out", "cross_replica_consistency_under_load",
    },
    AT.CONTROLLED_REDEPLOYMENT.value: {
        "availability_during_rollout", "request_loss_during_termination",
    },
    AT.OBSERVATION_ONLY.value: {
        "runtime_metric_availability", "end_to_end_trace_continuity",
        "runtime_retry_behavior", "error_signal_during_fault",
    },
    AT.JVM_DELAY.value: {
        "runtime_timeout_behavior", "upstream_slo_under_delay",
        "runtime_error_propagation", "fallback_behavior",
        "error_signal_during_fault",
    },
    AT.JVM_EXCEPTION.value: {
        "runtime_error_propagation", "fallback_behavior",
        "runtime_retry_behavior", "error_signal_during_fault",
        "upstream_slo_under_dependency_fault",
    },
    # 只读数据深挖：证据类型均取自现有契约词表
    AT.LOG_DEEP_DIVE.value: {
        "runtime_error_propagation", "runtime_retry_behavior",
        "runtime_timeout_behavior", "error_signal_during_fault",
        "fallback_behavior",
    },
    AT.TRACE_DRILLDOWN.value: {
        "end_to_end_trace_continuity", "error_span_attribution_during_fault",
        "runtime_timeout_behavior", "upstream_slo_under_delay",
    },
    AT.METRIC_DRILLDOWN.value: {
        "runtime_metric_availability", "resource_usage_under_load",
        "throughput_under_load_increase", "downstream_load_amplification",
    },
    AT.CONFIG_AUDIT.value: {
        "pod_restart_under_fault", "availability_during_rollout",
        "request_loss_during_termination",
    },
    AT.SOURCE_INSPECTION.value: {
        "fallback_behavior", "runtime_retry_behavior",
        "runtime_timeout_behavior",
    },
}

# 数据深挖动作（READONLY_ACTIONS 去掉 OBSERVATION_ONLY）
EXPLORATION_ACTIONS = frozenset(READONLY_ACTIONS - {AT.OBSERVATION_ONLY.value})

# 深挖动作仅对这些 Oracle 类型有判别力（无需故障注入即可判定）；
# fault_localization 属故障依赖型（FAULT_REQUIRED_ORACLES），不在此列
READONLY_ORACLES = frozenset({"trace_continuity", "error_signal_presence",
                              "metric_presence"})

# 成本（秒，含基线/恢复）与基础风险先验
ACTION_COST = {
    AT.OBSERVATION_ONLY.value: 10, AT.NETWORK_DELAY.value: 180,
    AT.NETWORK_LOSS.value: 180, AT.DEPENDENCY_UNAVAILABLE.value: 180,
    AT.POD_TERMINATION.value: 120, AT.CPU_PRESSURE.value: 180,
    AT.MEMORY_PRESSURE.value: 180, AT.LOAD_VARIATION.value: 300,
    AT.CONTROLLED_REDEPLOYMENT.value: 300,
    AT.JVM_DELAY.value: 180, AT.JVM_EXCEPTION.value: 180,
    AT.LOG_DEEP_DIVE.value: 40, AT.TRACE_DRILLDOWN.value: 40,
    AT.METRIC_DRILLDOWN.value: 30, AT.CONFIG_AUDIT.value: 30,
    AT.SOURCE_INSPECTION.value: 30,
}
ACTION_RISK = {
    AT.OBSERVATION_ONLY.value: 0.0, AT.NETWORK_DELAY.value: 0.20,
    AT.NETWORK_LOSS.value: 0.30, AT.DEPENDENCY_UNAVAILABLE.value: 0.35,
    AT.POD_TERMINATION.value: 0.30, AT.CPU_PRESSURE.value: 0.40,
    AT.MEMORY_PRESSURE.value: 0.50, AT.LOAD_VARIATION.value: 0.25,
    AT.CONTROLLED_REDEPLOYMENT.value: 0.50,
    AT.JVM_DELAY.value: 0.25, AT.JVM_EXCEPTION.value: 0.30,
    AT.LOG_DEEP_DIVE.value: 0.0, AT.TRACE_DRILLDOWN.value: 0.0,
    AT.METRIC_DRILLDOWN.value: 0.0, AT.CONFIG_AUDIT.value: 0.0,
    AT.SOURCE_INSPECTION.value: 0.0,
}

# 假设目标为上游时，故障应施加到其下游依赖的契约
FAULT_ON_DOWNSTREAM = {"RES-001", "RES-002", "RES-003", "RES-005",
                       "CONS-001", "CONS-002", "CONS-003", "OBS-004"}


def generate(hypotheses: list, graph, contracts_by_id: dict, cfg) -> list[dict]:
    candidates = []
    for h in hypotheses:
        if h.status not in ("RISK_UNCONFIRMED", "INCONCLUSIVE"):
            continue
        c = contracts_by_id.get(h.contract_id)
        if c is None:
            continue
        allowed = set(cfg.safety.get("allowed_actions", []))
        for action in c.actions:
            if action not in allowed or action not in ACTION_PROVIDES:
                continue
            if action in EXPLORATION_ACTIONS and not _oracle_compatible(c):
                continue
            for target, observe in _targets(h, action, graph, cfg):
                candidates.append({
                    "hypothesis_id": h.hypothesis_id,
                    "contract_id": h.contract_id,
                    "action_type": action,
                    "fault_target": target,        # 故障施加对象
                    "observe_target": observe,     # SLO 观测对象
                    "params": _params(action, h, graph, cfg),
                    "expected_evidence": sorted(
                        ACTION_PROVIDES[action] & set(h.missing_evidence)),
                    "provides": sorted(ACTION_PROVIDES[action]),
                    "estimated_cost": ACTION_COST[action],
                    "estimated_risk": _risk(action, target, graph),
                })
    return candidates


def _oracle_compatible(contract) -> bool:
    """深挖动作的 Oracle 兼容性门禁：仅只读可判定的契约才有深挖价值。"""
    otype = (getattr(contract, "oracle", None) or {}).get("type", "")
    return otype in READONLY_ORACLES


def _targets(h, action, graph, cfg):
    """(故障对象, 观测对象) 列表。受保护服务不作为故障对象。"""
    protected = set(cfg.safety.get("protected_services", []))
    svc = h.target
    if action in READONLY_ACTIONS:
        return [(svc, svc)]
    if action == AT.LOAD_VARIATION.value:
        return [(svc, svc)]
    if h.contract_id in FAULT_ON_DOWNSTREAM and action in (
            AT.NETWORK_DELAY.value, AT.NETWORK_LOSS.value,
            AT.DEPENDENCY_UNAVAILABLE.value):
        downs = [d for d in graph.downstream(svc)
                 if d not in protected and not graph.service_props(d).get("infra")]
        return [(d, svc) for d in downs] or [(svc, svc)]
    if svc in protected:
        return []
    return [(svc, svc)]


def _params(action, h, graph, cfg):
    max_dur = int(cfg.safety.get("max_fault_duration_s", 180))
    if action == AT.NETWORK_DELAY.value:
        # 延迟量级：足以超过常见默认超时，又不立即击穿一切
        return {"time_ms": 3000, "interface": "eth0", "duration_s": min(90, max_dur)}
    if action == AT.NETWORK_LOSS.value:
        return {"percent": 60, "interface": "eth0", "duration_s": min(90, max_dur)}
    if action == AT.DEPENDENCY_UNAVAILABLE.value:
        return {"percent": 100, "interface": "eth0", "duration_s": min(60, max_dur)}
    if action == AT.POD_TERMINATION.value:
        return {"evict_count": 1, "duration_s": min(60, max_dur)}
    if action == AT.CPU_PRESSURE.value:
        return {"cpu_percent": 80, "duration_s": min(90, max_dur)}
    if action == AT.MEMORY_PRESSURE.value:
        return {"mem_percent": 70, "duration_s": min(90, max_dur)}
    if action == AT.LOAD_VARIATION.value:
        return {"users": cfg.workload.get("users", 20) * 3,
                "duration_s": min(240, max_dur * 2)}
    if action == AT.CONTROLLED_REDEPLOYMENT.value:
        return {"duration_s": min(240, max_dur * 2)}
    if action == AT.JVM_DELAY.value:
        return {"classname": params_class(h), "methodname": params_method(h),
                "time_ms": 3000, "effect_count": 1, "duration_s": min(90, max_dur)}
    if action == AT.JVM_EXCEPTION.value:
        return {"classname": params_class(h), "methodname": params_method(h),
                "exception": "java.lang.RuntimeException",
                "exception_message": "cnaa controlled fault",
                "effect_count": 1, "duration_s": min(90, max_dur)}
    if action == AT.LOG_DEEP_DIVE.value:
        return {"lookback_s": 600, "tail_lines": 500, "keyword": ""}
    if action == AT.TRACE_DRILLDOWN.value:
        return {"lookback_s": 600, "limit": 20, "min_duration_ms": 0,
                "error_only": False}
    if action == AT.METRIC_DRILLDOWN.value:
        return {"lookback_s": 600}
    if action in (AT.CONFIG_AUDIT.value, AT.SOURCE_INSPECTION.value):
        return {}
    return {"lookback_s": 600}


def params_class(h) -> str:
    """JVM 故障类名：从假设证据或默认 Java 服务推断（需人工确认）。"""
    return getattr(h, "jvm_classname", "") or ""


def params_method(h) -> str:
    return getattr(h, "jvm_methodname", "") or ""


def _risk(action, target, graph):
    r = ACTION_RISK.get(action, 0.3)
    p = graph.service_props(target)
    rep = graph.latest_evidence(target, "replica_count", view="OBSERVED_CONFIG")
    n = (rep or {}).get("attrs", {}).get("replicas", 1)
    if n <= 1 and action not in READONLY_ACTIONS:
        r += 0.15                      # 单副本故障 = 完全中断风险
    if p.get("critical") and action not in READONLY_ACTIONS:
        r += 0.05
    return min(round(r, 3), 1.0)


# ---------- Agent 提议通道 ----------

# 允许 Agent 提议的字符串参数（白名单，数值参数走 PARAM_BOUNDS clamp）
_STR_PARAMS = {"classname", "methodname", "exception", "exception_message",
               "interface", "process", "keyword", "operation"}


def _clamp_params(proposed: dict, default: dict) -> dict:
    """参数规整：数值键 clamp 到 planner.PARAM_BOUNDS，字符串键走白名单。"""
    from agents.experiment_planner import PARAM_BOUNDS
    out = dict(default)
    for k, v in (proposed or {}).items():
        if k in PARAM_BOUNDS and isinstance(v, (int, float)):
            lo, hi = PARAM_BOUNDS[k]
            out[k] = max(lo, min(hi, v))
        elif k in _STR_PARAMS and isinstance(v, str) and v:
            out[k] = v
    return out


def normalize_proposal(prop: dict, hyps_by_id: dict, contracts_by_id: dict,
                       cfg, graph) -> dict | None:
    """把 Agent/followup 提议规整为与静态候选同构的 dict；非法提议返回 None。

    门禁：假设存在、动作 ∈ allowed_actions ∩ ACTION_PROVIDES、目标必须
    是图谱已知服务且故障对象非受保护服务；cost/risk/provides 一律查
    确定性表，不接受 Agent 自报估值（防止绕过 EGV 效用计算）。
    """
    h = hyps_by_id.get(prop.get("hypothesis_id"))
    action = prop.get("action_type")
    if h is None or action not in ACTION_PROVIDES:
        return None
    allowed = set(cfg.safety.get("allowed_actions", []))
    if action not in allowed or h.contract_id not in contracts_by_id:
        return None
    if action in EXPLORATION_ACTIONS and \
            not _oracle_compatible(contracts_by_id[h.contract_id]):
        return None
    ft, ot = prop.get("fault_target"), prop.get("observe_target")
    if not ot:
        return None
    # LLM 自由文本目标必须存在于图谱（静态枚举路径由 DEPENDS_ON 边隐式保证）
    services = set(graph.services())
    if ot not in services:
        return None
    if action in READONLY_ACTIONS:
        # 只读深挖无故障对象：LLM 常置 fault_target="none"/空/自指，
        # 统一规整为 observe_target（与静态 _targets 的 (svc,svc) 一致）；
        # 只读动作豁免 protected 门禁（safety_controller 同语义）。
        ft = ot
    else:
        if not ft or ft not in services:
            return None
        if ft in set(cfg.safety.get("protected_services", [])):
            return None
    params = _clamp_params(prop.get("params"), _params(action, h, graph, cfg))
    provides = sorted(ACTION_PROVIDES[action])
    return {
        "hypothesis_id": h.hypothesis_id,
        "contract_id": h.contract_id,
        "action_type": action,
        "fault_target": ft,
        "observe_target": ot,
        "params": params,
        "expected_evidence": sorted(set(provides) & set(h.missing_evidence)),
        "provides": provides,
        "estimated_cost": ACTION_COST[action],
        "estimated_risk": _risk(action, ft, graph),
        "source": "agent",
        "rationale": str(prop.get("rationale", ""))[:500],
    }