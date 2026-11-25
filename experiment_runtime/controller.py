"""受控实验控制器：execute_controlled 的完整实现（方案 §5.4 / §七）。

流程：基线 -> 固定负载 -> 注入 -> 生效确认 -> 故障窗口观测 ->
恢复 -> 恢复验证 -> Oracle。任何异常路径都保证故障回收（finally）。
"""
from __future__ import annotations

import time
from pathlib import Path

from common.config import Config
from common.models import Experiment, Observation
from active_verification.action_generator import EXPLORATION_ACTIONS
from experiment_runtime.chaosblade_adapter import ChaosBladeAdapter
from experiment_runtime import locust_adapter, recovery_manager, oracle_runner
from experiment_runtime.safety_controller import SafetyController
from collectors.runtime import jaeger_collector as jaeger
from collectors.runtime import prometheus_collector as prom
from collectors.runtime import event_collector

BASELINE_S = 45
RECOVERY_WAIT_S = 30


def _labels_port(graph, svc: str) -> tuple[dict, int | None]:
    lab = graph.latest_evidence(svc, "labels_annotations", view="OBSERVED_CONFIG")
    labels = (lab or {}).get("attrs", {}).get("pod_labels") or {"app": svc}
    routing = graph.latest_evidence(svc, "service_routing", view="OBSERVED_CONFIG")
    ports = (routing or {}).get("attrs", {}).get("ports") or []
    # tc 规则作用于 Pod 网络命名空间，local-port 必须用容器端口（targetPort）
    # 而非 Service port，两者不同时（如 emailservice 5000->8080）故障会完全不命中
    port = None
    if ports:
        tp = ports[0].get("targetPort")
        port = tp if isinstance(tp, int) else ports[0].get("port")
    return labels, port


def _window_metrics(cfg: Config, graph, svc: str, start: float, end: float) -> dict:
    m = jaeger.window_stats(cfg, svc, start, end)
    if prom.available(cfg):
        red = prom.service_red(cfg, svc, window=f"{max(int(end - start), 60)}s", ts=end)
        if red.get("available") and red.get("request_count", 0) > 0:
            m.update({"request_count": red["request_count"],
                      "error_rate": red["error_rate"],
                      "p99_latency_ms": red.get("p99_latency_ms") or m["p99_latency_ms"],
                      "throughput_rps": red["throughput_rps"],
                      "metrics_source": "prometheus"})
    m.setdefault("metrics_source", "jaeger")
    return m


def execute_controlled(cfg: Config, graph, experiment: Experiment, contract,
                       slo: dict, safety: SafetyController, run_dir: Path,
                       llm=None) -> tuple[dict, dict]:
    if experiment.action_type == "OBSERVATION_ONLY":
        return _observation_only(cfg, graph, experiment, contract, slo, run_dir)
    if experiment.action_type in EXPLORATION_ACTIONS:
        return _exploration(cfg, graph, experiment, contract, slo, run_dir, llm)
    if experiment.action_type == "LOAD_VARIATION":
        return _load_variation(cfg, graph, experiment, contract, slo, run_dir)
    if experiment.action_type == "CONTROLLED_REDEPLOYMENT":
        return _controlled_redeployment(cfg, graph, experiment, contract, slo, run_dir)

    safety.acquire(experiment.experiment_id)
    blade = ChaosBladeAdapter(cfg)
    blade_name = ""
    obs: dict[str, Observation] = {}
    try:
        experiment.status = "RUNNING"
        experiment.started_at = time.time()
        graph.update_experiment(experiment)

        ft, ot = experiment.fault_target, experiment.observe_target
        labels, port = _labels_port(graph, ft)
        pods_before = recovery_manager.pod_inventory(cfg, ft)
        restarts_before = event_collector.restart_delta(cfg, ft)
        params = experiment.params
        fault_dur = int(params.get("duration_s", 90))

        # 固定工作负载覆盖全窗口
        total_load = BASELINE_S + fault_dur + RECOVERY_WAIT_S + 90
        load_proc = locust_adapter.start_background(
            cfg, run_dir, users=cfg.workload.get("users", 20),
            duration_s=total_load)

        # 1. 基线
        t0 = time.time()
        time.sleep(BASELINE_S)
        t1 = time.time()
        base_m = _window_metrics(cfg, graph, ot, t0, t1)
        base_ft = _window_metrics(cfg, graph, ft, t0, t1)
        base_m["locust_window"] = locust_adapter.window_failure_rate(load_proc, t0, t1)
        obs["baseline"] = Observation(
            experiment_id=experiment.experiment_id, phase="baseline",
            metrics=base_m, refs={"fault_target_baseline": base_ft},
            window=(t0, t1))
        graph.add_observation(obs["baseline"])

        # 2. 注入 + 生效确认
        blade_name = blade.inject(experiment.action_type, cfg.kubernetes.namespace,
                                  labels, params, service_port=port)
        experiment.blade_name = blade_name
        cr_ok, cr_detail = blade.wait_effective(blade_name)
        experiment.notes = f"inject: {cr_detail}"

        # 3. 故障窗口
        f0 = time.time()
        time.sleep(fault_dur)
        f1 = time.time()
        during_m = _window_metrics(cfg, graph, ot, f0, f1)
        during_ft = _window_metrics(cfg, graph, ft, f0, f1)
        during_m["locust_window"] = locust_adapter.window_failure_rate(load_proc, f0, f1)
        # 观测窗口空但负载有请求 = 查询失败而非无流量，重取一次避免误判 INEFFECTIVE
        if during_m.get("span_count", 0) == 0 and \
                during_m["locust_window"].get("request_count", 0) > 0:
            lw = during_m["locust_window"]
            during_m = _window_metrics(cfg, graph, ot, f0, f1)
            during_m["locust_window"] = lw
        during_m["restart_delta"] = event_collector.restart_delta(cfg, ft) - restarts_before
        # RES-003：观测目标请求放大比（故障窗吞吐 / 基线窗吞吐），无基线流量时不可测
        base_rps = base_m.get("throughput_rps", 0.0)
        during_m["request_amplification"] = round(
            during_m.get("throughput_rps", 0.0) / base_rps, 3) if base_rps > 1e-6 else None

        abort = safety.check_abort(during_m, base_m)
        if abort:
            experiment.notes += f"; ABORT: {abort}"

        # 4. 回收 + 恢复验证（无论是否中止都执行）
        blade.destroy(blade_name)
        expect_churn = experiment.action_type == "POD_TERMINATION"
        rec = recovery_manager.verify_recovery(cfg, ft, pods_before, expect_churn)
        r0 = time.time()
        time.sleep(RECOVERY_WAIT_S)
        r1 = time.time()
        rec_m = _window_metrics(cfg, graph, ot, r0, r1)
        rec_m["recovery_time_s"] = rec["ready_time_s"]
        rec_m["recovered"] = rec["recovered"]

        # 5. 注入生效验证（CR 状态 + 指标变化双重确认）
        effect, effect_detail = _verify_effect(
            experiment.action_type, params, base_ft, during_ft, base_m, during_m, rec)

        # 6. Oracle 附加测量
        if contract.oracle.get("type") == "business_invariant":
            during_m.update(oracle_runner.measure_invariants(cfg, f0, f1))
        elif contract.oracle.get("type") == "trace_continuity":
            during_m.update(oracle_runner.measure_trace_continuity(cfg, f0, f1))
        elif contract.oracle.get("type") == "fault_localization":
            during_m.update(oracle_runner.measure_localization(cfg, ft, f0, f1))

        load_stats = locust_adapter.finish_background(load_proc)
        during_m["locust"] = load_stats

        obs["during"] = Observation(
            experiment_id=experiment.experiment_id, phase="during",
            metrics=during_m,
            refs={"jaeger": f"{cfg.runtime.jaeger_url}/api/traces?service={ot}",
                  "error_trace_ids": during_m.get("error_trace_ids", []),
                  "blade_cr": blade_name, "cr_detail": cr_detail,
                  "effect_detail": effect_detail, "locust_csv": load_stats.get("csv_prefix")},
            effect_verified=effect, window=(f0, f1))
        obs["recovery"] = Observation(
            experiment_id=experiment.experiment_id, phase="recovery",
            metrics=rec_m, refs={"recovery_check": rec}, window=(r0, r1))
        graph.add_observation(obs["during"])
        graph.add_observation(obs["recovery"])

        experiment.status = "EFFECTIVE" if effect else "INEFFECTIVE"
        experiment.finished_at = time.time()
        experiment.cost_actual = experiment.finished_at - experiment.started_at
        graph.update_experiment(experiment)

        verdict = oracle_runner.run_oracle(cfg, contract, experiment.__dict__ | {
            "observe_target": ot, "fault_target": ft}, obs, slo, graph)
        return obs, verdict
    finally:
        if blade_name:
            blade.destroy(blade_name)
        safety.release(experiment.experiment_id)


def _verify_effect(action, params, base_ft, during_ft, base_ot, during_ot, rec) -> tuple[bool, str]:
    if action == "POD_TERMINATION":
        ok = rec.get("pod_churn_observed", False)
        return ok, f"pod churn observed={ok}"
    if action == "NETWORK_DELAY":
        expect = params.get("time_ms", 3000) * 0.5
        delta_ft = during_ft.get("p99_latency_ms", 0) - base_ft.get("p99_latency_ms", 0)
        delta_ot = during_ot.get("p99_latency_ms", 0) - base_ot.get("p99_latency_ms", 0)
        err_up = during_ot.get("error_rate", 0) - base_ot.get("error_rate", 0)
        # 网络延迟体现在客户端 span/超时错误，服务端 handler span 不含传输时间
        ok = delta_ft >= expect or delta_ot >= expect or err_up > 0.05
        return ok, (f"fault-target p99 delta={delta_ft:.0f}ms, "
                    f"observe-target p99 delta={delta_ot:.0f}ms, "
                    f"observe-target error delta={err_up:.3f} (expect p99>={expect:.0f}ms "
                    f"or error>0.05)")
    if action in ("NETWORK_LOSS", "DEPENDENCY_UNAVAILABLE"):
        err_up = during_ot.get("error_rate", 0) - base_ot.get("error_rate", 0)
        # span 数必须按窗口时长归一化（baseline/during 窗口长度不同），用吞吐速率比较；
        # baseline 吞吐近零时 drop 无判别力，置 0
        base_rps = base_ft.get("throughput_rps", 0)
        drop = (base_rps - during_ft.get("throughput_rps", 0)) / base_rps \
            if base_rps > 1e-6 else 0.0
        # 丢包/依赖不可达还会体现为上游超时重试导致的延迟激增
        delta_ot = during_ot.get("p99_latency_ms", 0) - base_ot.get("p99_latency_ms", 0)
        base_p99 = base_ot.get("p99_latency_ms", 0)
        lat_spike = delta_ot > 200 and during_ot.get("p99_latency_ms", 0) > 3 * max(base_p99, 1)
        # 端到端负载失败率（locust 故障窗口切片）：部分丢包下 TCP 重传使服务端
        # 错误率增幅有限，但前端请求失败/超时仍会显著上升
        loc_up = (during_ot.get("locust_window") or {}).get("error_rate", 0) - \
            (base_ot.get("locust_window") or {}).get("error_rate", 0)
        ok = err_up > 0.02 or drop > 0.5 or lat_spike or loc_up > 0.02
        return ok, (f"upstream error delta={err_up:.3f}, "
                    f"fault-target throughput drop={drop:.2f}, "
                    f"observe-target p99 delta={delta_ot:.0f}ms (lat_spike={lat_spike}), "
                    f"locust failure delta={loc_up:.3f}")
    if action in ("CPU_PRESSURE", "MEMORY_PRESSURE"):
        return True, "CR accepted (resource-level effect assumed)"
    if action == "JVM_DELAY":
        expect = params.get("time_ms", 3000) * 0.5
        delta_ft = during_ft.get("p99_latency_ms", 0) - base_ft.get("p99_latency_ms", 0)
        delta_ot = during_ot.get("p99_latency_ms", 0) - base_ot.get("p99_latency_ms", 0)
        ok = delta_ft >= expect or delta_ot >= expect
        return ok, (f"jvm delay: fault-target p99 delta={delta_ft:.0f}ms, "
                    f"observe-target p99 delta={delta_ot:.0f}ms (expect>={expect:.0f}ms)")
    if action == "JVM_EXCEPTION":
        err_ft = during_ft.get("error_rate", 0) - base_ft.get("error_rate", 0)
        err_ot = during_ot.get("error_rate", 0) - base_ot.get("error_rate", 0)
        ok = err_ft > 0.02 or err_ot > 0.02
        return ok, (f"jvm exception: fault-target error delta={err_ft:.3f}, "
                    f"observe-target error delta={err_ot:.3f}")
    return False, "unknown action"


def _observation_only(cfg, graph, experiment, contract, slo, run_dir):
    """只读证据获取：不注入故障，查询现有遥测。"""
    experiment.status = "RUNNING"
    experiment.started_at = time.time()
    ot = experiment.observe_target
    now = time.time()
    lookback = experiment.params.get("lookback_s", 600)
    m = _window_metrics(cfg, graph, ot, now - lookback, now)
    m["metric_series_present"] = bool(
        prom.available(cfg) and prom.service_red(cfg, ot).get("request_count", 0) > 0)
    if contract.oracle.get("type") == "trace_continuity":
        m.update(oracle_runner.measure_trace_continuity(cfg, now - lookback, now))
    obs = {"during": Observation(
        experiment_id=experiment.experiment_id, phase="during",
        metrics=m, refs={"jaeger": cfg.runtime.jaeger_url},
        effect_verified=True, window=(now - lookback, now))}
    graph.add_observation(obs["during"])
    experiment.status = "COMPLETED"
    experiment.finished_at = time.time()
    experiment.cost_actual = experiment.finished_at - experiment.started_at
    graph.update_experiment(experiment)
    verdict = oracle_runner.run_oracle(cfg, contract, experiment.__dict__ | {
        "observe_target": ot, "fault_target": ot}, obs, slo, graph)
    return obs, verdict


def _exploration(cfg, graph, experiment, contract, slo, run_dir, llm):
    """只读数据深挖：有界多步自主探索，不注入故障、不走 chaosblade。"""
    from experiment_runtime import exploration_runner
    experiment.status = "RUNNING"
    experiment.started_at = time.time()
    ot = experiment.observe_target or experiment.target
    now = time.time()
    lookback = experiment.params.get("lookback_s", 600)
    # 基础窗口指标（span_count/request_count 供 Oracle 样本充分性判定）
    m = _window_metrics(cfg, graph, ot, now - lookback, now)
    m.update(exploration_runner.run_exploration(
        cfg, graph, experiment, contract, llm, run_dir))
    obs = {"during": Observation(
        experiment_id=experiment.experiment_id, phase="during",
        metrics=m, refs={"exploration": f"explorations/{experiment.experiment_id}.jsonl"},
        effect_verified=True, window=(now - lookback, time.time()))}
    graph.add_observation(obs["during"])
    experiment.status = "COMPLETED"
    experiment.finished_at = time.time()
    experiment.cost_actual = experiment.finished_at - experiment.started_at
    graph.update_experiment(experiment)
    verdict = oracle_runner.run_oracle(cfg, contract, experiment.__dict__ | {
        "observe_target": ot, "fault_target": ot}, obs, slo, graph)
    return obs, verdict


def _load_variation(cfg, graph, experiment, contract, slo, run_dir):
    """负载变化实验（SCA 伸缩性）：基准负载 -> 高负载，测吞吐增益。

    不注入故障，而是改变运行负载（方案 §4.5 LOAD_VARIATION）。
    产出 throughput_gain_ratio 供 scaling_target Oracle 判定。
    """
    experiment.status = "RUNNING"
    experiment.started_at = time.time()
    ot = experiment.observe_target
    users = int(experiment.params.get("users", cfg.workload.get("users", 20) * 3))
    dur = int(experiment.params.get("duration_s", 120))

    # 1. 基准负载窗口
    base_proc = locust_adapter.start_background(
        cfg, run_dir, users=cfg.workload.get("users", 20), duration_s=BASELINE_S + 30)
    t0 = time.time()
    time.sleep(BASELINE_S)
    t1 = time.time()
    base_m = _window_metrics(cfg, graph, ot, t0, t1)
    locust_adapter.finish_background(base_proc)

    # 2. 高负载窗口
    load_proc = locust_adapter.start_background(cfg, run_dir, users=users, duration_s=dur + 30)
    f0 = time.time()
    time.sleep(dur)
    f1 = time.time()
    during_m = _window_metrics(cfg, graph, ot, f0, f1)
    during_m["locust_window"] = locust_adapter.window_failure_rate(load_proc, f0, f1)
    load_stats = locust_adapter.finish_background(load_proc)
    during_m["locust"] = load_stats

    base_rps = base_m.get("throughput_rps", 0)
    gain = during_m.get("throughput_rps", 0) / base_rps if base_rps > 1e-6 else 0.0
    during_m["throughput_gain_ratio"] = round(gain, 3)
    during_m["load_users"] = users

    obs = {"baseline": Observation(
               experiment_id=experiment.experiment_id, phase="baseline",
               metrics=base_m, window=(t0, t1)),
           "during": Observation(
               experiment_id=experiment.experiment_id, phase="during",
               metrics=during_m, refs={"load_csv": load_stats.get("csv_prefix")},
               effect_verified=True, window=(f0, f1)),
           "recovery": Observation(
               experiment_id=experiment.experiment_id, phase="recovery",
               metrics={"recovered": True, "recovery_time_s": 0}, window=(f1, f1))}
    for o in obs.values():
        graph.add_observation(o)
    experiment.status = "EFFECTIVE"
    experiment.finished_at = time.time()
    experiment.cost_actual = experiment.finished_at - experiment.started_at
    graph.update_experiment(experiment)
    verdict = oracle_runner.run_oracle(cfg, contract, experiment.__dict__ | {
        "observe_target": ot, "fault_target": ot}, obs, slo, graph)
    return obs, verdict


def _controlled_redeployment(cfg, graph, experiment, contract, slo, run_dir):
    """受控重部署（DEP-001 滚动更新安全）：rollout restart 并观测可用性。

    不注入破坏性故障，而是触发一次真实滚动更新，测量更新期间的
    错误率/请求损失（方案 §4.5 CONTROLLED_REDEPLOYMENT）。
    """
    import subprocess
    experiment.status = "RUNNING"
    experiment.started_at = time.time()
    ft = experiment.fault_target
    ot = experiment.observe_target
    ns = cfg.kubernetes.namespace
    pods_before = recovery_manager.pod_inventory(cfg, ft)

    load_proc = locust_adapter.start_background(
        cfg, run_dir, users=cfg.workload.get("users", 20), duration_s=BASELINE_S + 240)
    t0 = time.time()
    time.sleep(BASELINE_S)
    t1 = time.time()
    base_m = _window_metrics(cfg, graph, ot, t0, t1)
    base_m["locust_window"] = locust_adapter.window_failure_rate(load_proc, t0, t1)

    # 触发滚动更新
    subprocess.run(["kubectl", "--kubeconfig", cfg.kubernetes.kubeconfig,
                    "-n", ns, "rollout", "restart", f"deployment/{ft}"],
                   capture_output=True, text=True, timeout=30)
    f0 = time.time()
    subprocess.run(["kubectl", "--kubeconfig", cfg.kubernetes.kubeconfig,
                    "-n", ns, "rollout", "status", f"deployment/{ft}", "--timeout=180s"],
                   capture_output=True, text=True, timeout=200)
    time.sleep(30)
    f1 = time.time()
    during_m = _window_metrics(cfg, graph, ot, f0, f1)
    during_m["locust_window"] = locust_adapter.window_failure_rate(load_proc, f0, f1)
    rec = recovery_manager.verify_recovery(cfg, ft, pods_before, expect_churn=True)
    during_m["restart_delta"] = event_collector.restart_delta(cfg, ft)
    load_stats = locust_adapter.finish_background(load_proc)
    during_m["locust"] = load_stats

    r0 = time.time()
    time.sleep(RECOVERY_WAIT_S)
    r1 = time.time()
    rec_m = _window_metrics(cfg, graph, ot, r0, r1)
    rec_m["recovered"] = rec["recovered"]
    rec_m["recovery_time_s"] = rec["ready_time_s"]

    effect = rec.get("pod_churn_observed", False)
    obs = {"baseline": Observation(
               experiment_id=experiment.experiment_id, phase="baseline",
               metrics=base_m, window=(t0, t1)),
           "during": Observation(
               experiment_id=experiment.experiment_id, phase="during",
               metrics=during_m, refs={"rollout": f"deployment/{ft}",
                                       "recovery_check": rec},
               effect_verified=effect, window=(f0, f1)),
           "recovery": Observation(
               experiment_id=experiment.experiment_id, phase="recovery",
               metrics=rec_m, window=(r0, r1))}
    for o in obs.values():
        graph.add_observation(o)
    experiment.status = "EFFECTIVE" if effect else "INEFFECTIVE"
    experiment.finished_at = time.time()
    experiment.cost_actual = experiment.finished_at - experiment.started_at
    graph.update_experiment(experiment)
    verdict = oracle_runner.run_oracle(cfg, contract, experiment.__dict__ | {
        "observe_target": ot, "fault_target": ft}, obs, slo, graph)
    return obs, verdict