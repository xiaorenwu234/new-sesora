"""Jaeger 采集器：调用依赖、聚合指标窗口、异常 Trace 引用。

只把聚合摘要与引用写入图谱，原始 Trace 保留在 Jaeger（方案 §2.2）。
同时为 Oracle Runner 提供按时间窗口的统计查询能力。
"""
from __future__ import annotations

import time

import requests

from common.config import Config
from common.models import Evidence

ERROR_TAG_KEYS = {"error", "otel.status_code", "rpc.grpc.status_code", "http.status_code"}


def _get(cfg: Config, path: str, params: dict | None = None, timeout=30):
    url = cfg.runtime.jaeger_url.rstrip("/") + path
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def available(cfg: Config) -> bool:
    try:
        _get(cfg, "/api/services", timeout=5)
        return True
    except Exception:
        return False


def list_services(cfg: Config) -> list[str]:
    return _get(cfg, "/api/services").get("data", []) or []


def _is_error_span(span: dict) -> bool:
    for t in span.get("tags", []):
        k, v = t.get("key"), t.get("value")
        if k == "error" and v in (True, "true"):
            return True
        if k == "otel.status_code" and v == "ERROR":
            return True
        if k == "rpc.grpc.status_code" and str(v) not in ("0", "None"):
            return True
        if k == "http.status_code":
            try:
                if int(v) >= 500:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def fetch_traces(cfg: Config, service: str, start_s: float | None = None,
                 end_s: float | None = None, limit: int | None = None) -> list[dict]:
    params = {"service": service, "limit": limit or cfg.runtime.get("trace_limit", 200)}
    if start_s and end_s:
        params.update({"start": int(start_s * 1e6), "end": int(end_s * 1e6),
                       "lookback": "custom"})
    else:
        params["lookback"] = cfg.runtime.get("lookback", "1h")
    # 高流量服务（如 frontend）的窗口查询可能超过 30s；静默返回空会直接
    # 导致生效验证误判 INEFFECTIVE，必须重试
    for attempt in range(3):
        try:
            return _get(cfg, "/api/traces", params, timeout=60).get("data", []) or []
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return []


def aggregate(traces: list[dict]) -> dict:
    """聚合 Trace -> 每服务统计 + 服务依赖边 + 错误样例。"""
    stats: dict[str, dict] = {}
    deps: dict[tuple, int] = {}
    error_traces: dict[str, list] = {}
    for tr in traces:
        proc_svc = {pid: p.get("serviceName", "?")
                    for pid, p in tr.get("processes", {}).items()}
        span_svc, span_by_id = {}, {}
        for sp in tr.get("spans", []):
            svc = proc_svc.get(sp.get("processID"), "?")
            span_svc[sp["spanID"]] = svc
            span_by_id[sp["spanID"]] = sp
            s = stats.setdefault(svc, {"spans": 0, "errors": 0, "durations_ms": []})
            s["spans"] += 1
            s["durations_ms"].append(sp.get("duration", 0) / 1000.0)
            if _is_error_span(sp):
                s["errors"] += 1
                error_traces.setdefault(svc, [])
                if len(error_traces[svc]) < 5:
                    error_traces[svc].append(tr.get("traceID"))
        for sp in tr.get("spans", []):
            for ref in sp.get("references", []):
                if ref.get("refType") == "CHILD_OF":
                    parent = span_svc.get(ref.get("spanID"))
                    child = span_svc.get(sp["spanID"])
                    if parent and child and parent != child:
                        deps[(parent, child)] = deps.get((parent, child), 0) + 1
    for svc, s in stats.items():
        ds = sorted(s["durations_ms"])
        s["error_rate"] = s["errors"] / s["spans"] if s["spans"] else 0.0
        s["p50_latency_ms"] = ds[int(len(ds) * 0.5)] if ds else 0.0
        s["p99_latency_ms"] = ds[min(int(len(ds) * 0.99), len(ds) - 1)] if ds else 0.0
        s.pop("durations_ms")
    return {"stats": stats, "dependencies": deps, "error_traces": error_traces,
            "trace_count": len(traces)}


def window_stats(cfg: Config, service: str, start_s: float, end_s: float,
                 limit: int = 500) -> dict:
    """Oracle 用：指定时间窗口内某服务的错误率/延迟/吞吐与错误 Trace 引用。"""
    traces = fetch_traces(cfg, service, start_s, end_s, limit)
    agg = aggregate(traces)
    s = agg["stats"].get(service, {"spans": 0, "errors": 0, "error_rate": 0.0,
                                   "p50_latency_ms": 0.0, "p99_latency_ms": 0.0})
    duration = max(end_s - start_s, 1e-6)
    return {"service": service, "window": [start_s, end_s],
            "span_count": s["spans"], "error_count": s["errors"],
            "error_rate": s["error_rate"],
            "p50_latency_ms": s["p50_latency_ms"],
            "p99_latency_ms": s["p99_latency_ms"],
            "throughput_rps": s["spans"] / duration,
            "error_trace_ids": agg["error_traces"].get(service, []),
            "trace_sample_count": len(traces),
            "dependencies": {f"{a}->{b}": n for (a, b), n in agg["dependencies"].items()}}


def collect(cfg: Config, profile: dict) -> dict:
    if not available(cfg):
        return {"evidence": [], "available": False,
                "note": "Jaeger 不可达，运行时证据缺失（记录为覆盖率缺口而非否定证据）"}
    now = time.time()
    lookback_s = _parse_lookback(cfg.runtime.get("lookback", "1h"))
    start = now - lookback_s
    otel_names = {m["otel_name"]: svc for svc, m in
                  (profile.get("service_mapping") or {}).items() if m.get("otel_name")}
    all_traces = []
    for otel_name in otel_names:
        all_traces += fetch_traces(cfg, otel_name, start, now)
    # 去重（同一 Trace 会被多个服务查询返回）
    uniq = {t["traceID"]: t for t in all_traces}
    agg = aggregate(list(uniq.values()))
    evs: list[Evidence] = []
    sid = cfg.system_id

    for (a, b), n in agg["dependencies"].items():
        sa, sb = otel_names.get(a), otel_names.get(b)
        if not sa or not sb:
            continue  # 未解析服务身份不强行关联
        evs.append(Evidence(
            kind="service_dependency", layer="runtime", subject=sa,
            attrs={"downstream": sb, "call_count": n, "window_s": lookback_s},
            source="jaeger", method="CHILD_OF span aggregation",
            location=f"jaeger:{cfg.runtime.jaeger_url}/api/traces?service={a}",
            system_id=sid))
    for otel_name, s in agg["stats"].items():
        svc = otel_names.get(otel_name)
        if not svc:
            continue
        evs.append(Evidence(
            kind="metric_window", layer="runtime", subject=svc,
            attrs=s | {"window_start": start, "window_end": now},
            source="jaeger", method="span aggregation",
            location=f"jaeger:service={otel_name}", system_id=sid))
        for tid in agg["error_traces"].get(otel_name, []):
            evs.append(Evidence(
                kind="trace_ref", layer="runtime", subject=svc,
                attrs={"trace_id": tid, "error": True},
                source="jaeger", method="error span sampling",
                location=f"{cfg.runtime.jaeger_url}/trace/{tid}", system_id=sid))
    evs.append(Evidence(
        kind="trace_coverage", layer="runtime", subject=cfg.system_id,
        attrs={"sampled_traces": agg["trace_count"], "window_s": lookback_s,
               "note": "sampling-based; absence of calls is not proof of no calls"},
        source="jaeger", method="coverage metadata", system_id=sid))
    return {"evidence": evs, "available": True, "aggregate": agg}


def _parse_lookback(s: str) -> float:
    unit = s[-1]
    try:
        v = float(s[:-1])
    except ValueError:
        return 3600
    return v * {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 3600)