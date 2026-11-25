"""只读数据深挖工具集（ExplorationRunner 的工具白名单）。

权限边界：全部只读——kubectl 仅 get/list/logs 等价操作、Prometheus 仅
query/query_range、Jaeger 仅 GET、源码限制在受测仓库内。无通用 Shell，
keyword/operation 等自由文本仅在 Python 侧过滤，不进入任何命令行。

每个工具返回 {"ok", "result", "metrics", "evidence_id"}：
- result   结构化摘要（供 LLM 决定下一步，截断由 runner 负责）
- metrics  确定性解析器从原始输出计算的指标增量（不接受 LLM 自报）
- evidence 原始发现构造 Evidence 入图谱（带来源与采集方法）
"""
from __future__ import annotations

import re
import time

from common.config import Config
from common.models import Evidence
from collectors.runtime import jaeger_collector as jaeger
from collectors.runtime import prometheus_collector as prom

# k8s_inspect 允许的资源类型（全部只读）
K8S_KINDS = frozenset({"events", "deployment", "service", "configmap", "pod", "hpa"})

_ERROR_LOG_RE = re.compile(r"\b(error|exception|panic|fatal|traceback)\b", re.I)


class ExplorationTools:
    def __init__(self, cfg: Config, graph, agent_tools=None):
        self.cfg = cfg
        self.graph = graph
        self.agent_tools = agent_tools
        self._apis = None

    # ---------- 统一入口 ----------
    def call(self, tool: str, args: dict) -> dict:
        fn = getattr(self, f"_t_{tool}", None)
        if fn is None or tool.startswith("_"):
            return {"ok": False, "error": f"unknown tool {tool}", "metrics": {}}
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return {"ok": False, "error": f"bad args: {e}", "metrics": {}}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300], "metrics": {}}

    def _emit(self, kind: str, layer: str, subject: str, attrs: dict,
              location: str, method: str) -> str:
        ev = Evidence(kind=kind, layer=layer, subject=subject, attrs=attrs,
                      source="exploration_runner", method=method,
                      location=location, system_id=self.cfg.system_id)
        self.graph.add_evidence(ev)
        return ev.evidence_id

    def _k8s(self):
        if self._apis is None:
            from collectors.deployment.k8s_collector import load_apis
            self._apis = load_apis(self.cfg)
        return self._apis

    # ---------- 1. Pod 日志 ----------
    def _t_pod_logs(self, service: str, tail_lines: int = 500,
                    since_s: int = 600, keyword: str = "") -> dict:
        if not service:
            return {"ok": False, "error": "service required", "metrics": {}}
        tail_lines = max(50, min(int(tail_lines), 2000))
        since_s = max(60, min(int(since_s), 3600))
        core, _, _, _ = self._k8s()
        ns = self.cfg.kubernetes.namespace
        pods = core.list_namespaced_pod(ns, label_selector=f"app={service}").items
        running = [p for p in pods if (p.status.phase or "") == "Running"]
        if not running:
            return {"ok": False, "error": f"no running pod for {service}",
                    "metrics": {}}
        pod = running[0]
        raw = core.read_namespaced_pod_log(
            pod.metadata.name, ns, tail_lines=tail_lines,
            since_seconds=since_s) or ""
        lines = raw.splitlines()
        if keyword:
            lines = [l for l in lines if keyword.lower() in l.lower()]
        err_lines = [l for l in lines if _ERROR_LOG_RE.search(l)]
        metrics = {"log_line_count": len(lines), "error_log_count": len(err_lines)}
        eid = self._emit(
            "log_excerpt", "runtime", service,
            {"excerpt": "\n".join(lines[:60])[:2000],
             "error_excerpt": "\n".join(err_lines[:20])[:1500],
             "keyword": keyword, **metrics},
            location=f"pod/{pod.metadata.name}", method="k8s pod log read")
        return {"ok": True, "metrics": metrics, "evidence_id": eid,
                "result": {"pod": pod.metadata.name, "lines": len(lines),
                           "error_lines": err_lines[:15],
                           "sample": lines[:10]}}

    # ---------- 2. Trace 钻取 ----------
    def _t_trace_search(self, service: str, operation: str = "",
                        min_duration_ms: int = 0, error_only: bool = False,
                        limit: int = 20, lookback_s: int = 600) -> dict:
        if not service:
            return {"ok": False, "error": "service required", "metrics": {}}
        limit = max(1, min(int(limit), 50))
        lookback_s = max(60, min(int(lookback_s), 3600))
        now = time.time()
        traces = jaeger.fetch_traces(self.cfg, service, now - lookback_s, now,
                                     limit=limit)
        err_spans, broken, matched = 0, 0, []
        for tr in traces:
            ids = {sp["spanID"] for sp in tr.get("spans", [])}
            is_broken = any(
                ref.get("refType") == "CHILD_OF" and ref.get("spanID") not in ids
                for sp in tr.get("spans", []) for ref in sp.get("references", []))
            broken += int(is_broken)
            proc = {pid: p.get("serviceName", "?")
                    for pid, p in tr.get("processes", {}).items()}
            tr_err = 0
            for sp in tr.get("spans", []):
                if jaeger._is_error_span(sp):
                    tr_err += 1
                dur_ms = sp.get("duration", 0) / 1000.0
                if (not operation or operation.lower() in sp.get("operationName", "").lower()) \
                        and dur_ms >= min_duration_ms \
                        and (not error_only or jaeger._is_error_span(sp)):
                    matched.append({"trace_id": tr.get("traceID"),
                                    "service": proc.get(sp.get("processID")),
                                    "operation": sp.get("operationName"),
                                    "duration_ms": round(dur_ms, 1),
                                    "error": jaeger._is_error_span(sp)})
            err_spans += tr_err
        ratio = broken / len(traces) if traces else 0.0
        metrics = {"trace_count": len(traces), "error_span_count": err_spans,
                   "broken_trace_ratio": round(ratio, 4)}
        eid = self._emit(
            "trace_drilldown", "runtime", service,
            {"matched_spans": matched[:30], "operation": operation,
             "error_only": error_only, **metrics},
            location=(matched[0]["trace_id"] if matched else f"service/{service}"),
            method="jaeger trace search")
        return {"ok": True, "metrics": metrics, "evidence_id": eid,
                "result": {"traces": len(traces), "error_spans": err_spans,
                           "broken_trace_ratio": metrics["broken_trace_ratio"],
                           "matched": matched[:15]}}

    def _t_trace_detail(self, trace_id: str) -> dict:
        if not trace_id or not re.fullmatch(r"[0-9a-fA-F]{1,32}", trace_id):
            return {"ok": False, "error": "invalid trace_id", "metrics": {}}
        data = jaeger._get(self.cfg, f"/api/traces/{trace_id}").get("data", [])
        if not data:
            return {"ok": False, "error": "trace not found", "metrics": {}}
        tr = data[0]
        proc = {pid: p.get("serviceName", "?")
                for pid, p in tr.get("processes", {}).items()}
        spans, err = [], 0
        for sp in tr.get("spans", []):
            e = jaeger._is_error_span(sp)
            err += int(e)
            spans.append({"service": proc.get(sp.get("processID")),
                          "operation": sp.get("operationName"),
                          "duration_ms": round(sp.get("duration", 0) / 1000.0, 1),
                          "error": e})
        spans.sort(key=lambda s: -s["duration_ms"])
        metrics = {"error_span_count": err}
        eid = self._emit("trace_drilldown", "runtime",
                         spans[0]["service"] if spans else "unknown",
                         {"spans": spans[:40], **metrics},
                         location=trace_id, method="jaeger trace detail")
        return {"ok": True, "metrics": metrics, "evidence_id": eid,
                "result": {"span_count": len(spans), "error_spans": err,
                           "slowest": spans[:12]}}

    # ---------- 3. 自由 PromQL ----------
    def _t_prom_query(self, promql: str, window_s: int = 600) -> dict:
        if not promql or len(promql) > 500:
            return {"ok": False, "error": "promql required (<=500 chars)",
                    "metrics": {}}
        window_s = max(60, min(int(window_s), 3600))
        import requests
        base = self.cfg.runtime.prometheus_url.rstrip("/")
        now = time.time()
        r = requests.get(base + "/api/v1/query_range",
                         params={"query": promql, "start": now - window_s,
                                 "end": now, "step": max(15, window_s // 60)},
                         timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "success":
            return {"ok": False, "error": data.get("error", "query failed")[:200],
                    "metrics": {}}
        series = data["data"]["result"]
        summary = []
        for s in series[:10]:
            vals = [float(v[1]) for v in s.get("values", []) if v[1] != "NaN"]
            summary.append({"metric": s.get("metric", {}),
                            "last": vals[-1] if vals else None,
                            "max": max(vals) if vals else None,
                            "points": len(vals)})
        metrics = {"metric_series_present": bool(series), "series_count": len(series)}
        eid = self._emit("metric_query", "runtime",
                         str(series[0].get("metric", {}).get("service_name", "cluster"))
                         if series else "cluster",
                         {"promql": promql, "series": summary, **metrics},
                         location=promql, method="prometheus query_range")
        return {"ok": True, "metrics": metrics, "evidence_id": eid,
                "result": {"series": summary}}

    # ---------- 4. K8s 事件与配置 ----------
    def _t_k8s_inspect(self, kind: str, name: str = "") -> dict:
        if kind not in K8S_KINDS:
            return {"ok": False, "error": f"kind {kind} not allowed", "metrics": {}}
        core, apps, autoscaling, _ = self._k8s()
        ns = self.cfg.kubernetes.namespace
        metrics: dict = {}
        if kind == "events":
            sel = f"involvedObject.name={name}" if name else None
            evs = core.list_namespaced_event(ns, field_selector=sel).items
            warns = [e for e in evs if (e.type or "") == "Warning"]
            metrics = {"warning_event_count": len(warns), "event_count": len(evs)}
            result = {"events": [{"reason": e.reason, "type": e.type,
                                  "object": e.involved_object.name,
                                  "message": (e.message or "")[:150],
                                  "count": e.count} for e in evs[-30:]]}
            subject = name or ns
        elif kind == "pod":
            pods = ([core.read_namespaced_pod(name, ns)] if name
                    else core.list_namespaced_pod(ns).items)
            restarts = sum(cs.restart_count or 0
                           for p in pods for cs in (p.status.container_statuses or []))
            metrics = {"restart_count": restarts, "pod_count": len(pods)}
            result = {"pods": [{"name": p.metadata.name, "phase": p.status.phase,
                                "restarts": sum(cs.restart_count or 0 for cs in
                                                (p.status.container_statuses or []))}
                               for p in pods[:20]]}
            subject = name or ns
        elif kind == "deployment":
            deps = ([apps.read_namespaced_deployment(name, ns)] if name
                    else apps.list_namespaced_deployment(ns).items)
            missing = []
            for d in deps:
                for ct in d.spec.template.spec.containers:
                    if ct.readiness_probe is None or ct.liveness_probe is None:
                        missing.append(f"{d.metadata.name}/{ct.name}")
            metrics = {"probe_missing_count": len(missing),
                       "deployment_count": len(deps)}
            result = {"deployments": [{"name": d.metadata.name,
                                       "replicas": d.spec.replicas,
                                       "ready": d.status.ready_replicas or 0}
                                      for d in deps[:20]],
                      "probe_missing": missing}
            subject = name or ns
        elif kind == "hpa":
            hpas = ([autoscaling.read_namespaced_horizontal_pod_autoscaler(name, ns)]
                    if name else
                    autoscaling.list_namespaced_horizontal_pod_autoscaler(ns).items)
            result = {"hpas": [{"name": h.metadata.name,
                                "min": h.spec.min_replicas, "max": h.spec.max_replicas,
                                "current": h.status.current_replicas} for h in hpas]}
            metrics = {"hpa_count": len(hpas)}
            subject = name or ns
        else:  # service / configmap
            read = getattr(core, f"read_namespaced_{kind}")
            lst = getattr(core, f"list_namespaced_{kind}")
            objs = [read(name, ns)] if name else lst(ns).items
            result = {kind: [{"name": o.metadata.name} for o in objs[:20]]}
            if kind == "configmap" and name:
                result["data_keys"] = list((objs[0].data or {}).keys())
            metrics = {f"{kind}_count": len(objs)}
            subject = name or ns
        eid = self._emit("config_finding", "deployment", subject,
                         {"kind": kind, **result, **metrics},
                         location=f"{kind}/{name or '*'}", method="k8s read-only inspect")
        return {"ok": True, "metrics": metrics, "evidence_id": eid, "result": result}

    # ---------- 5. 源码核对 ----------
    def _t_source_inspect(self, service: str, file: str = "",
                          max_lines: int = 200) -> dict:
        if self.agent_tools is None:
            from agents.tools import AgentTools
            self.agent_tools = AgentTools(self.cfg, self.graph, {})
        out = self.agent_tools.inspect_source(service, file or None,
                                              max_lines=max(50, min(int(max_lines), 400)))
        if out.get("error"):
            return {"ok": False, "error": out["error"], "metrics": {}}
        metrics = {"source_lines_read": len(out.get("content", "").splitlines())}
        eid = self._emit("source_finding", "source", service,
                         {"file": file, "content": out.get("content", "")[:2500],
                          **metrics},
                         location=file or f"service/{service}",
                         method="repository source read")
        return {"ok": True, "metrics": metrics, "evidence_id": eid, "result": out}


# 工具清单说明（供 LLM prompt 与参数校验）
TOOL_SPECS = {
    "pod_logs": "查服务 Pod 日志。args: service(必填), tail_lines(50-2000), "
                "since_s(60-3600), keyword(过滤词，如 timeout/retry/exception)",
    "trace_search": "按服务筛 trace。args: service(必填), operation, "
                    "min_duration_ms, error_only(bool), limit(1-50), lookback_s",
    "trace_detail": "钻取单条 trace 的 span 链路。args: trace_id(必填)",
    "prom_query": "自由 PromQL 区间查询。args: promql(必填,<=500字符), window_s",
    "k8s_inspect": "K8s 只读审查。args: kind(events|deployment|service|configmap|pod|hpa), name(可选)",
    "source_inspect": "受测仓库源码查看。args: service(必填), file(仓库相对路径), max_lines",
}