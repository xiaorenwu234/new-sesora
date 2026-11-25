"""Prometheus 采集器：RED 指标窗口与资源指标（spanmetrics 链路）。

指标名兼容 OTel Collector spanmetrics connector 的两种命名：
traces_span_metrics_calls_total / calls_total。
"""
from __future__ import annotations

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


def collect(cfg: Config, profile: dict) -> dict:
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
    return {"evidence": evs, "available": True, "calls_metric": metric}