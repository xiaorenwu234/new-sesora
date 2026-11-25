"""Locust 适配器：受控业务工作负载（固定场景，统一实验条件）。"""
from __future__ import annotations

import csv
import subprocess
import time
from pathlib import Path

from common.config import Config, REPO_ROOT


def run_workload(cfg: Config, run_dir: Path, users: int | None = None,
                 duration_s: int | None = None, spawn_rate: int | None = None) -> dict:
    """headless 运行 Locust，返回汇总统计（客户端视角的 Oracle 辅助证据）。"""
    wl = cfg.workload
    users = users or wl.get("users", 20)
    duration_s = duration_s or wl.get("duration_s", 120)
    spawn_rate = spawn_rate or wl.get("spawn_rate", 5)
    locust_file = REPO_ROOT / wl.get("locust_file", "benchmarks/workloads/boutique_workload.py")
    prefix = str(Path(run_dir) / f"locust-{int(time.time())}")
    cmd = ["locust", "-f", str(locust_file), "--headless",
           "-u", str(users), "-r", str(spawn_rate),
           "-t", f"{duration_s}s", "--host", wl["frontend_url"],
           "--csv", prefix, "--only-summary", "--loglevel", "ERROR"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=duration_s + 120)
    stats = _parse_csv(prefix + "_stats.csv")
    stats["exit_code"] = proc.returncode
    stats["users"] = users
    stats["duration_s"] = duration_s
    stats["csv_prefix"] = prefix
    if proc.returncode != 0:
        stats["error"] = (proc.stderr or "")[-500:]
    return stats


def _parse_csv(path: str) -> dict:
    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return {"available": False}
    total = next((r for r in rows if r.get("Name") == "Aggregated"), None)
    if not total:
        return {"available": False}
    req = int(total.get("Request Count", 0))
    fail = int(total.get("Failure Count", 0))
    return {"available": True, "request_count": req, "failure_count": fail,
            "error_rate": fail / req if req else 0.0,
            "rps": float(total.get("Requests/s", 0) or 0),
            "p99_ms": float(total.get("99%", 0) or 0),
            "p95_ms": float(total.get("95%", 0) or 0),
            "median_ms": float(total.get("Median Response Time", 0) or 0)}


def start_background(cfg: Config, run_dir: Path, users: int, duration_s: int,
                     spawn_rate: int = 5) -> subprocess.Popen:
    """后台启动负载（覆盖 基线+故障+恢复 全窗口），由调用方负责等待结束。"""
    wl = cfg.workload
    locust_file = REPO_ROOT / wl.get("locust_file", "benchmarks/workloads/boutique_workload.py")
    prefix = str(Path(run_dir) / f"locust-bg-{int(time.time())}")
    cmd = ["locust", "-f", str(locust_file), "--headless",
           "-u", str(users), "-r", str(spawn_rate),
           "-t", f"{duration_s}s", "--host", wl["frontend_url"],
           "--csv", prefix, "--only-summary", "--loglevel", "ERROR"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    proc.csv_prefix = prefix  # type: ignore[attr-defined]
    return proc


def finish_background(proc: subprocess.Popen, timeout: float = 300) -> dict:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=30)
    return _parse_csv(proc.csv_prefix + "_stats.csv")  # type: ignore[attr-defined]


def window_failure_rate(proc: subprocess.Popen, start: float, end: float) -> dict:
    """从 stats_history 的累计计数按时间窗口切片失败率（运行中即可读）。

    全程汇总会稀释故障窗口的失败信号，生效验证必须用窗口内的增量计数。
    """
    path = getattr(proc, "csv_prefix", "") + "_stats_history.csv"
    try:
        with open(path) as f:
            rows = [r for r in csv.DictReader(f) if r.get("Name") == "Aggregated"]
    except (FileNotFoundError, OSError):
        return {"available": False}

    def at(ts):
        cand = [r for r in rows if int(r["Timestamp"]) <= ts]
        return cand[-1] if cand else None

    a, b = at(start), at(end)
    if not a or not b:
        return {"available": False}
    req = int(b["Total Request Count"]) - int(a["Total Request Count"])
    fail = int(b["Total Failure Count"]) - int(a["Total Failure Count"])
    return {"available": req > 0, "request_count": req, "failure_count": fail,
            "error_rate": fail / req if req else 0.0}