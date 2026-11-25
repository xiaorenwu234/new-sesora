"""Prometheus 采集器：RED 指标窗口与资源指标（spanmetrics 链路）。

指标名兼容 OTel Collector spanmetrics connector 的两种命名：
traces_span_metrics_calls_total / calls_total。
"""
from __future__ import annotations

import math
import time

import requests

from common.config import Config
from common.models import Evidence


def _query(cfg: Config, promql: str, ts: float | None = None):
    url = cfg.runtime.prometheus_url.rstrip("/") + "/api/v1/query"
    params = {"query": promql}
    if ts:
        params["time"] = ts
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(data.get("error", "prometheus query failed"))
    return data["data"]["result"]


def _range(cfg, query, now):
    url = cfg.runtime.prometheus_url.rstrip("/") + "/api/v1/query_range"
    response = requests.get(url, params={"query": query, "start": now - 900,
                                         "end": now, "step": 60}, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise ValueError(payload.get("error", "range query failed"))
    for series in payload["data"]["result"]:
        points = [(int(ts), float(value)) for ts, value in series["values"]]
        yield series["metric"], [(ts, value) for ts, value in points if math.isfinite(value)]


def architecture_metrics(cfg, profile, snapshot, now, calls_metric):
    namespace = cfg.kubernetes.namespace
    mapping = profile.get("service_mapping") or {}
    aliases = {spec.get("otel_name") or service: service for service, spec in mapping.items()}
    deployments = {spec.get("deployment") or service: service for service, spec in mapping.items()}
    queries = {}
    selector = f'namespace="{namespace}",container!="",container!="POD"'
    queries["cpu"] = f"rate(container_cpu_usage_seconds_total{{{selector}}}[5m])"
    queries["memory"] = f"container_memory_working_set_bytes{{{selector}}}"
    if calls_metric:
        rate = f"sum by (service_name) (rate({calls_metric}[5m]))"
        errors = f'sum by (service_name) (rate({calls_metric}{{status_code="STATUS_CODE_ERROR"}}[5m]))'
        queries.update(error_rate=f"({errors} or 0 * {rate}) / {rate}",
                       latency="histogram_quantile(0.99, sum by (service_name, le) "
                       "(rate(traces_span_metrics_duration_milliseconds_bucket[5m])))")
    resources, red, errors = {}, {}, {}
    for metric, query in queries.items():
        try:
            rows = list(_range(cfg, query, now))
        except (requests.RequestException, ValueError, KeyError) as error:
            errors[metric] = str(error)
            continue
        for labels, values in rows:
            if not values:
                continue
            if metric in {"cpu", "memory"}:
                pod, container = labels.get("pod"), labels.get("container")
                owner = snapshot.get("pods", {}).get(pod, {}).get("owner")
                service = deployments.get(owner)
                if not service or not container:
                    continue
                by_pod = resources.setdefault((service, container), {}).setdefault(metric, {})
                for ts, value in values:
                    by_pod[pod, ts] = max(value, by_pod.get((pod, ts), 0))
            else:
                service = aliases.get(labels.get("service_name"))
                if service:
                    red.setdefault(service, {})[metric] = values
    evidence = []
    for (service, container), metrics in resources.items():
        attrs = {"container": container}
        for metric, samples in metrics.items():
            totals, counts = {}, {}
            for (_, ts), value in samples.items():
                totals[ts] = totals.get(ts, 0) + value
                counts[ts] = counts.get(ts, 0) + 1
            averages = [total / counts[ts] for ts, total in totals.items()]
            attrs[metric] = {"mean": sum(averages) / len(averages), "peak": max(samples.values())}
        evidence.append(Evidence("container_usage", "runtime", service, attrs, "prometheus",
                                 "container metrics over 15 minutes", cfg.runtime.prometheus_url,
                                 system_id=cfg.system_id, collected_at=now))
    for service, metrics in red.items():
        evidence.append(Evidence("red_timeseries", "runtime", service, metrics, "prometheus",
                                 "aligned RED metrics over 15 minutes", cfg.runtime.prometheus_url,
                                 system_id=cfg.system_id, collected_at=now))
    return evidence, errors


def available(cfg: Config) -> bool:
    try:
        r = requests.get(cfg.runtime.prometheus_url.rstrip("/") + "/-/ready", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _calls_metric(cfg: Config) -> str | None:
    for name in ("traces_span_metrics_calls_total", "calls_total",
                 "traces_spanmetrics_calls_total"):
        try:
            if _query(cfg, f"count({name})"):
                return name
        except Exception:
            continue
    return None


def service_red(cfg: Config, service: str, window: str = "5m",
                ts: float | None = None) -> dict:
    """Oracle 用：某服务窗口内错误率 / p99 延迟 / 吞吐（PromQL 聚合）。"""
    metric = _calls_metric(cfg)
    if not metric:
        return {"available": False}
    label = f'service_name="{service}"'
    total = _query(cfg, f'sum(increase({metric}{{{label}}}[{window}]))', ts)
    errors = _query(cfg,
                    f'sum(increase({metric}{{{label},status_code="STATUS_CODE_ERROR"}}[{window}]))', ts)
    t = float(total[0]["value"][1]) if total else 0.0
    e = float(errors[0]["value"][1]) if errors else 0.0
    p99 = _query(cfg,
                 f'histogram_quantile(0.99, sum by (le) (rate('
                 f'traces_span_metrics_duration_milliseconds_bucket{{{label}}}[{window}])))', ts)
    return {"available": True, "request_count": t, "error_count": e,
            "error_rate": e / t if t else 0.0,
            "p99_latency_ms": float(p99[0]["value"][1]) if p99 else None,
            "throughput_rps": t / _window_seconds(window),
            "promql": f"sum(increase({metric}{{{label}}}[{window}]))"}


def _window_seconds(w: str) -> float:
    return float(w[:-1]) * {"s": 1, "m": 60, "h": 3600}.get(w[-1], 60)


def collect(cfg: Config, profile: dict, snapshot: dict | None = None) -> dict:
    if not available(cfg):
        return {"evidence": [], "available": False,
                "note": "Prometheus 不可达，指标证据缺失（覆盖率缺口）"}
    metric = _calls_metric(cfg)
    evs: list[Evidence] = []
    now = time.time()
    if metric:
        for svc in (profile.get("service_mapping") or {}):
            red = service_red(cfg, svc, "10m", now)
            if red.get("available") and red.get("request_count", 0) > 0:
                evs.append(Evidence(
                    kind="metric_window", layer="runtime", subject=svc,
                    attrs=red | {"window_end": now, "window_s": 600},
                    source="prometheus", method="spanmetrics PromQL",
                    location=red["promql"], system_id=cfg.system_id))
            evs.append(Evidence(
                kind="metrics_endpoint", layer="runtime", subject=svc,
                attrs={"series_present": red.get("request_count", 0) > 0},
                source="prometheus", method="series presence check",
                location=cfg.runtime.prometheus_url, system_id=cfg.system_id))
    errors = {}
    if snapshot is not None:
        architecture, errors = architecture_metrics(cfg, profile, snapshot, now, metric)
        evs.extend(architecture)
    return {"evidence": evs, "available": True, "calls_metric": metric,
            "architecture_metric_errors": errors}
