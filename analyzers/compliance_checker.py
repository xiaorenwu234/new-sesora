"""确定性合规检查器（Module C 第一项任务）。

对每条契约的 static_check 在 Evidence Graph 上执行确定性判定：
- 部署配置类证据是权威事实 -> 可直接判 VIOLATED / COMPLIANT；
- 源码模式类证据缺失只是弱否定 -> 判 RISK_UNCONFIRMED 交给假设流程；
- 运行时证据缺失 -> 不判 VIOLATED（覆盖率缺口原则）。
"""
from __future__ import annotations

import networkx as nx

from common.models import ContractState as CS
from analyzers import architecture

SEVERITY_WEIGHT = {"critical": 1.0, "high": 0.8, "medium": 0.5, "low": 0.2}


def _ids(evs):
    return [e["evidence_id"] for e in evs if e.get("evidence_id")]


def check(graph, contract, svc: str) -> dict:
    """返回 {state, evidence_ids, detail}。"""
    fn = CHECKS.get(contract.static_check)
    if fn is None:
        return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": [],
                "detail": f"no deterministic check for {contract.static_check}"}
    return fn(graph, contract, svc)


# ---------- 各规则实现 ----------

def _resource_limits(g, c, svc):
    evs = g.evidence(subject=svc, kind="resource_configuration", view="OBSERVED_CONFIG")
    if not evs:
        return {"state": CS.INCONCLUSIVE.value, "evidence_ids": [],
                "detail": "no observed deployment evidence"}
    missing = []
    for e in evs:
        a = e.get("attrs", {})
        if not a.get("limits") or not a.get("requests"):
            missing.append(a.get("container", "?"))
    if missing:
        return {"state": CS.VIOLATED.value, "evidence_ids": _ids(evs),
                "detail": f"containers missing requests/limits: {missing}"}
    return {"state": CS.COMPLIANT.value, "evidence_ids": _ids(evs),
            "detail": "all containers define requests and limits"}


def _probes_replicas(g, c, svc):
    evs = g.evidence(subject=svc, kind="probe_configuration", view="OBSERVED_CONFIG")
    if not evs:
        return {"state": CS.INCONCLUSIVE.value, "evidence_ids": [],
                "detail": "no probe evidence"}
    missing = [e["attrs"].get("container", "?") for e in evs
               if not e["attrs"].get("readiness") or not e["attrs"].get("liveness")]
    if missing:
        return {"state": CS.VIOLATED.value, "evidence_ids": _ids(evs),
                "detail": f"containers missing readiness/liveness probes: {missing}"}
    return {"state": CS.COMPLIANT.value, "evidence_ids": _ids(evs),
            "detail": "readiness and liveness probes configured"}


def _scaling_config(g, c, svc):
    rep = g.latest_evidence(svc, "replica_count", view="OBSERVED_CONFIG")
    hpa = g.latest_evidence(svc, "hpa_configuration", view="OBSERVED_CONFIG")
    ids = _ids([x for x in (rep, hpa) if x])
    if hpa:
        return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
                "detail": "HPA configured"}
    n = (rep or {}).get("attrs", {}).get("replicas", 1)
    if n and n > 1:
        return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
                "detail": f"static replicas={n}"}
    return {"state": CS.VIOLATED.value, "evidence_ids": ids,
            "detail": "single replica without HPA"}


def _no_local_state(g, c, svc):
    evs = g.evidence(subject=svc, kind="volume_configuration", view="OBSERVED_CONFIG")
    bad = [e for e in evs if e.get("attrs", {}).get("host_path")]
    if bad:
        return {"state": CS.VIOLATED.value, "evidence_ids": _ids(bad),
                "detail": "hostPath volume breaks state isolation"}
    return {"state": CS.COMPLIANT.value, "evidence_ids": _ids(evs),
            "detail": "no hostPath volumes"}


def _rolling_safe(g, c, svc):
    probes = g.evidence(subject=svc, kind="probe_configuration", view="OBSERVED_CONFIG")
    strat = g.latest_evidence(svc, "update_strategy", view="OBSERVED_CONFIG")
    ids = _ids(probes + ([strat] if strat else []))
    has_readiness = any(e["attrs"].get("readiness") for e in probes)
    if not has_readiness:
        return {"state": CS.VIOLATED.value, "evidence_ids": ids,
                "detail": "rolling update without readiness gate"}
    return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
            "detail": "readiness gate present for rolling update"}


def _graceful_term(g, c, svc):
    life = g.evidence(subject=svc, kind="lifecycle_configuration", view="OBSERVED_CONFIG")
    term = g.latest_evidence(svc, "termination_configuration", view="OBSERVED_CONFIG")
    ids = _ids(life + ([term] if term else []))
    if any(e["attrs"].get("pre_stop") for e in life):
        return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
                "detail": "preStop hook configured"}
    if g.has_evidence(svc, "signal_handling", layer="source"):
        return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
                "detail": "source-level signal handling found"}
    return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": ids,
            "detail": "no preStop hook; in-code SIGTERM handling unknown"}


def _config_externalized(g, c, svc):
    cfg_evs = g.evidence(subject=svc, kind="config_source", layer="source")
    env_evs = g.evidence(subject=svc, kind="env_configuration", view="OBSERVED_CONFIG")
    ids = _ids(cfg_evs + env_evs)
    if cfg_evs and any(e["attrs"].get("env") for e in env_evs):
        return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
                "detail": "configuration read from environment"}
    hard = g.evidence(subject=svc, kind="hardcoded_config_evidence", layer="source")
    if hard and not cfg_evs:
        return {"state": CS.VIOLATED.value, "evidence_ids": _ids(hard),
                "detail": f"{len(hard)} hardcoded endpoints without env config"}
    return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": ids,
            "detail": "config externalization partially evidenced"}


def _desired_observed(g, c, svc):
    diffs = []
    ids = []
    for kind in ("replica_count", "resource_configuration", "probe_configuration"):
        des = g.evidence(subject=svc, kind=kind, view="DESIRED_CONFIG")
        obs = g.evidence(subject=svc, kind=kind, view="OBSERVED_CONFIG")
        ids += _ids(des + obs)
        if not des or not obs:
            continue
        d_attrs = [e["attrs"] for e in des]
        o_attrs = [e["attrs"] for e in obs]
        if kind == "replica_count":
            if d_attrs[0].get("replicas") != o_attrs[0].get("replicas"):
                diffs.append(f"replicas desired={d_attrs[0].get('replicas')} "
                             f"observed={o_attrs[0].get('replicas')}")
        elif kind == "resource_configuration":
            dk = {(a.get("container"), str(a.get("limits"))) for a in d_attrs}
            ok = {(a.get("container"), str(a.get("limits"))) for a in o_attrs}
            if dk != ok:
                diffs.append("resource limits drift")
    img_des = g.evidence(subject=svc, kind="container_image", view="DESIRED_CONFIG")
    img_obs = g.evidence(subject=svc, kind="container_image", view="OBSERVED_CONFIG")
    ids += _ids(img_des + img_obs)
    if img_des and img_obs:
        def _split(img):
            base = img.split("/")[-1]
            name, _, tag = base.partition(":")
            return name, tag or None
        d_pairs = {_split(e["attrs"].get("image", "")) for e in img_des}
        o_pairs = {_split(e["attrs"].get("image", "")) for e in img_obs}
        d_names = {n for n, _ in d_pairs}
        o_names = {n for n, _ in o_pairs}
        # manifest 常由 kustomize 在构建时注入 tag：无 tag 一侧只比较仓库名
        drift = d_names != o_names or any(
            dt and ot and dt != ot
            for dn, dt in d_pairs for on, ot in o_pairs if dn == on)
        if drift:
            diffs.append(f"image drift desired={sorted(d_names)} observed={sorted(o_names)}")
    if diffs:
        return {"state": CS.VIOLATED.value, "evidence_ids": ids,
                "detail": "; ".join(diffs)}
    return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
            "detail": "desired and observed configurations consistent"}


def _trace_instrumentation(g, c, svc):
    src = g.evidence(subject=svc, kind="trace_instrumentation", layer="source")
    mw = g.evidence(subject=svc, kind="metric_window", layer="runtime")
    ids = _ids(src + mw)
    if mw:  # 运行时已有该服务 Span，插桩被运行证据确认
        return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
                "detail": "runtime spans observed for service"}
    if src:
        return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
                "detail": "instrumentation code present (runtime unconfirmed)"}
    return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": ids,
            "detail": "no instrumentation evidence; auto-instrumentation possible"}


def _error_logging(g, c, svc):
    evs = g.evidence(subject=svc, kind="error_logging", layer="source")
    if evs:
        return {"state": CS.COMPLIANT.value, "evidence_ids": _ids(evs),
                "detail": f"{len(evs)} error-logging sites"}
    return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": [],
            "detail": "no error logging pattern found"}


def _metrics_coverage(g, c, svc):
    evs = g.evidence(subject=svc, kind="metrics_endpoint", layer="runtime")
    if not evs:
        return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": [],
                "detail": "runtime metrics unavailable (coverage gap, not violation)"}
    ok = all(e["attrs"].get("series_present") for e in evs)
    return {"state": CS.COMPLIANT.value if ok else CS.VIOLATED.value,
            "evidence_ids": _ids(evs),
            "detail": "RED metric series present" if ok else "no metric series"}


def _timeout_calls(g, c, svc):
    deps = g.downstream(svc)
    if not deps:
        return {"state": CS.NOT_APPLICABLE.value, "evidence_ids": [],
                "detail": "no downstream dependency"}
    evs = g.evidence(subject=svc, kind="timeout_configuration", layer="source")
    dep_evs = g.evidence(subject=svc, kind="service_dependency")
    ids = _ids(evs + dep_evs)
    if evs:
        return {"state": CS.COMPLIANT.value, "evidence_ids": ids,
                "detail": f"timeout patterns found ({len(evs)} sites), deps={deps}"}
    return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": ids,
            "detail": f"no timeout pattern for calls to {deps}"}


def _exception_handling(g, c, svc):
    evs = g.evidence(subject=svc, kind="exception_handling", layer="source")
    if evs:
        return {"state": CS.COMPLIANT.value, "evidence_ids": _ids(evs),
                "detail": f"{len(evs)} exception-handling sites"}
    return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": [],
            "detail": "no exception handling pattern found"}


def _retry_bounded(g, c, svc):
    evs = g.evidence(subject=svc, kind="retry_policy", layer="source")
    if not evs:
        return {"state": CS.NOT_APPLICABLE.value, "evidence_ids": [],
                "detail": "no retry mechanism detected"}
    # 命中重试模式但无法静态确认上限/退避 -> 待验证
    return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": _ids(evs),
            "detail": "retry pattern present; boundedness/backoff unverified"}


def _idempotency(g, c, svc):
    evs = g.evidence(subject=svc, kind="idempotency_evidence", layer="source")
    if evs:
        return {"state": CS.COMPLIANT.value, "evidence_ids": _ids(evs),
                "detail": "idempotency/dedup pattern present"}
    return {"state": CS.RISK_UNCONFIRMED.value, "evidence_ids": [],
            "detail": "write operations without idempotency evidence"}


def _no_cycles(g, c, svc):
    dg = g.service_dependency_graph()
    cycles = [s for s in nx.strongly_connected_components(dg) if len(s) > 1]
    involved = [s for s in cycles if svc in s]
    if involved:
        return {"state": CS.VIOLATED.value, "evidence_ids": [],
                "detail": f"dependency cycle: {sorted(involved[0])}"}
    return {"state": CS.COMPLIANT.value, "evidence_ids": [],
            "detail": "service not in any dependency cycle"}


CHECKS = {
    **architecture.CHECKS,
    "resource_limits_required": _resource_limits,
    "probes_and_replicas": _probes_replicas,
    "scaling_config_present": _scaling_config,
    "no_local_state": _no_local_state,
    "rolling_update_safe": _rolling_safe,
    "graceful_termination": _graceful_term,
    "config_externalized": _config_externalized,
    "desired_observed_consistency": _desired_observed,
    "trace_instrumentation_present": _trace_instrumentation,
    "error_logging_present": _error_logging,
    "metrics_coverage": _metrics_coverage,
    "timeout_on_client_calls": _timeout_calls,
    "exception_handling_on_calls": _exception_handling,
    "retry_bounded": _retry_bounded,
    "idempotency_evidence_present": _idempotency,
    "no_dependency_cycles": _no_cycles,
}
