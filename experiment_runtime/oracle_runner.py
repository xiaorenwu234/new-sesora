"""验证 Oracle Runner：独立于 Agent 的确定性判定（方案 §七 步骤 8）。

阈值只来自冻结 SLO 文件；严格区分：
- 注入未生效 / 观测不足 / 未复现故障  -> INCONCLUSIVE（不算反驳）
- 生效且充分观测下越过阈值            -> VIOLATED
- 生效且充分观测下满足阈值            -> COMPLIANT
"""
from __future__ import annotations

from common.config import Config
from common.models import ContractState as CS
from active_verification.action_generator import EXPLORATION_ACTIONS
from collectors.runtime import jaeger_collector as jaeger

MIN_SAMPLES = 20          # 窗口内最少 Span 样本量
FAULT_REQUIRED_ORACLES = {"slo_violation", "business_invariant", "fault_localization",
                          "error_signal_presence", "scaling_target"}
# 契约指标名 -> 遥测窗口字段名
METRIC_ALIASES = {"p99_latency": "p99_latency_ms", "p50_latency": "p50_latency_ms"}


def _m(metrics: dict, name: str):
    if name in metrics:
        return metrics[name]
    return metrics.get(METRIC_ALIASES.get(name, ""))


def run_oracle(cfg: Config, contract, experiment: dict, obs: dict,
               slo: dict, graph) -> dict:
    """obs: {"baseline": Observation, "during": Observation, "recovery": Observation}"""
    otype = contract.oracle.get("type", "slo_violation")
    during = obs["during"].metrics if obs.get("during") else {}
    baseline = obs["baseline"].metrics if obs.get("baseline") else {}
    recovery = obs["recovery"].metrics if obs.get("recovery") else {}
    target = experiment["observe_target"]
    action = experiment["action_type"]

    reasons, comparisons = [], []
    sufficient = during.get("span_count", 0) >= MIN_SAMPLES or \
        during.get("request_count", 0) >= MIN_SAMPLES
    if not sufficient:
        reasons.append(f"insufficient samples in fault window "
                       f"(spans={during.get('span_count', 0)})")
    if action == "OBSERVATION_ONLY" and otype in FAULT_REQUIRED_ORACLES:
        sufficient = False
        reasons.append("observation-only cannot confirm fault-dependent hypothesis")
    # 只读深挖的 error_signal_presence 专用语义：自然窗口检到错误信号 ->
    # 可观测性成立；未检到 -> 无错误样本不能断言"吞异常"，需注入确认
    if action in EXPLORATION_ACTIONS and otype == "error_signal_presence" \
            and during.get("error_count", 0) == 0:
        sufficient = False
        reasons.append("no error samples in read-only window; "
                       "fault injection needed to confirm")

    violated = False
    thresholds = contract.oracle_thresholds(slo)
    resolved = {}
    for metric, th in thresholds.items():
        key = th if not isinstance(th, dict) else th
        # slo_key 中 {target} 替换为观测对象
        resolved[metric] = _lookup(slo, contract, metric, target)

    if otype == "slo_violation":
        for metric, th in resolved.items():
            if th is None:
                continue
            val = _m(during, metric)
            if metric == "recovery_time_s":
                val = recovery.get("recovery_time_s")
            if metric == "restart_count":
                val = during.get("restart_delta", 0)
            if val is None:
                reasons.append(f"metric {metric} unavailable")
                sufficient = False
                continue
            bad = val > th
            base_val = _m(baseline, metric)
            comparisons.append({"metric": metric, "value": val, "threshold": th,
                                "exceeded": bad,
                                "baseline": base_val})
            if bad:
                violated = True
                if base_val is not None and base_val > th:
                    reasons.append(f"{metric} already violated at baseline "
                                   f"(pre-existing, attribution caution)")
    elif otype == "business_invariant":
        inv = during.get("invariant_violations")
        th = resolved.get("invariant_violations", 0)
        if inv is None:
            sufficient = False
            reasons.append("business invariant not measurable from current telemetry")
        else:
            comparisons.append({"metric": "invariant_violations", "value": inv,
                                "threshold": th, "exceeded": inv > th})
            violated = inv > th
        er_th = resolved.get("error_rate")
        if er_th is not None and during.get("error_rate") is not None:
            bad = during["error_rate"] > er_th
            comparisons.append({"metric": "error_rate", "value": during["error_rate"],
                                "threshold": er_th, "exceeded": bad})
            violated = violated or bad
    elif otype == "trace_continuity":
        ratio = during.get("broken_trace_ratio")
        th = resolved.get("broken_trace_ratio", 0.05)
        if ratio is None:
            sufficient = False
            reasons.append("trace continuity not measured")
        else:
            comparisons.append({"metric": "broken_trace_ratio", "value": ratio,
                                "threshold": th, "exceeded": ratio > th})
            violated = ratio > th
    elif otype == "error_signal_presence":
        required = resolved.get("error_signal_detected", True)
        detected = during.get("error_count", 0) > 0
        comparisons.append({"metric": "error_signal_detected", "value": detected,
                            "threshold": required, "exceeded": required and not detected})
        violated = required and not detected
    elif otype == "metric_presence":
        present = during.get("metric_series_present", False)
        required = resolved.get("metric_series_present", True)
        comparisons.append({"metric": "metric_series_present", "value": present,
                            "threshold": required, "exceeded": required and not present})
        violated = required and not present
        sufficient = True   # 存在性检查不需要大样本
    elif otype == "fault_localization":
        acc = during.get("localization_accuracy")
        th = resolved.get("localization_accuracy", 0.8)
        if acc is None:
            sufficient = False
            reasons.append("localization accuracy not measured")
        else:
            comparisons.append({"metric": "localization_accuracy", "value": acc,
                                "threshold": th, "exceeded": acc < th})
            violated = acc < th
    elif otype == "scaling_target":
        gain = during.get("throughput_gain_ratio")
        th = resolved.get("throughput_gain_ratio", 1.5)
        if gain is None:
            sufficient = False
            reasons.append("scaling gain not measured")
        else:
            comparisons.append({"metric": "throughput_gain_ratio", "value": gain,
                                "threshold": th, "exceeded": gain < th})
            violated = gain < th
    else:
        sufficient = False
        reasons.append(f"oracle type {otype} not executable dynamically")

    if not obs["during"].effect_verified and otype in FAULT_REQUIRED_ORACLES:
        state = CS.INCONCLUSIVE.value
        reasons.append("fault effect not verified")
    elif not sufficient:
        state = CS.INCONCLUSIVE.value
    elif violated:
        state = CS.VIOLATED.value
    else:
        state = CS.COMPLIANT.value

    return {"state": state, "violated": state == CS.VIOLATED.value,
            "sufficient": sufficient, "reasons": reasons,
            "comparisons": comparisons, "oracle_type": otype}


def _lookup(slo: dict, contract, metric: str, target: str):
    spec = contract.oracle.get("metrics", {}).get(metric)
    key = spec.get("slo_key") if isinstance(spec, dict) else spec
    if not key:
        return None
    key = str(key).replace("{target}", target)
    node = slo
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return None
    return node


def measure_invariants(cfg: Config, start_s: float, end_s: float) -> dict:
    """业务不变量（Trace 推导）：一次成功下单必须恰好一次成功扣款。

    检查 checkoutservice PlaceOrder Trace 内 PaymentService/Charge Span 数：
    >1 => 重复扣款（重试一致性缺陷的直接证据）。
    """
    traces = jaeger.fetch_traces(cfg, "checkoutservice", start_s, end_s, limit=500)
    violations, checked = [], 0
    for tr in traces:
        proc_svc = {pid: p.get("serviceName", "?")
                    for pid, p in tr.get("processes", {}).items()}
        ops = [(proc_svc.get(sp.get("processID")), sp.get("operationName", ""))
               for sp in tr.get("spans", [])]
        has_order = any("PlaceOrder" in op for _, op in ops)
        if not has_order:
            continue
        checked += 1
        charges = [op for svc, op in ops if "Charge" in op]
        if len(charges) > 1:
            violations.append({"trace_id": tr.get("traceID"),
                               "charge_count": len(charges),
                               "invariant": "one_charge_per_order"})
    return {"invariant_violations": len(violations), "orders_checked": checked,
            "details": violations[:10]}


def measure_trace_continuity(cfg: Config, start_s: float, end_s: float) -> dict:
    """断链率：Trace 中存在引用了缺失 Span 的 CHILD_OF（上下文传播断裂）。"""
    services = jaeger.list_services(cfg)
    seen, broken = set(), 0
    for svc in services[:12]:
        for tr in jaeger.fetch_traces(cfg, svc, start_s, end_s, limit=100):
            tid = tr.get("traceID")
            if tid in seen:
                continue
            seen.add(tid)
            ids = {sp["spanID"] for sp in tr.get("spans", [])}
            for sp in tr.get("spans", []):
                for ref in sp.get("references", []):
                    if ref.get("refType") == "CHILD_OF" and ref.get("spanID") not in ids:
                        broken += 1
                        break
                else:
                    continue
                break
    return {"broken_trace_ratio": broken / len(seen) if seen else 0.0,
            "traces_checked": len(seen)}


def measure_localization(cfg: Config, fault_target: str, start_s: float,
                         end_s: float) -> dict:
    """错误 Span 归因率：故障窗口内错误 Span 属于故障服务的比例。"""
    total_err, target_err = 0, 0
    for svc in jaeger.list_services(cfg)[:12]:
        traces = jaeger.fetch_traces(cfg, svc, start_s, end_s, limit=200)
        for tr in traces:
            proc_svc = {pid: p.get("serviceName", "?")
                        for pid, p in tr.get("processes", {}).items()}
            for sp in tr.get("spans", []):
                if jaeger._is_error_span(sp):
                    total_err += 1
                    if proc_svc.get(sp.get("processID")) == fault_target:
                        target_err += 1
    return {"localization_accuracy": target_err / total_err if total_err else None,
            "error_spans": total_err, "target_error_spans": target_err}