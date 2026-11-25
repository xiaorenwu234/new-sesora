"""安全控制器（自研，独立于 Agent 与选择算法，方案 §5.4 强调项）。

职责：
1. 候选动作准入审查（命名空间/动作类型/保护服务/爆炸半径/时长）；
2. 执行期守护（并发=1、故障时长硬上限、紧急中止）；
3. 所有拒绝决定留痕，供审计与论文有效性分析。
"""
from __future__ import annotations

import threading
import time

from common.config import Config
from common.models import ActionType as AT, READONLY_ACTIONS


class SafetyViolation(Exception):
    pass


class SafetyController:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = cfg.safety
        self.decisions: list[dict] = []
        self._lock = threading.Lock()
        self._active: str | None = None

    # ---------- 准入审查 ----------
    def validate_candidate(self, cand: dict, graph) -> tuple[bool, str]:
        action = cand["action_type"]
        target = cand["fault_target"]
        ns = self.cfg.kubernetes.namespace
        reasons = []
        if ns not in self.s.get("allowed_namespaces", []):
            reasons.append(f"namespace {ns} not allowed")
        if action not in self.s.get("allowed_actions", []):
            reasons.append(f"action {action} not allowed")
        if action not in READONLY_ACTIONS:
            if target in self.s.get("protected_services", []):
                reasons.append(f"target {target} is protected")
            if graph.service_props(target).get("infra"):
                reasons.append(f"target {target} is infrastructure")
            if target not in graph.services():
                reasons.append(f"target {target} not a resolved service")
        params = cand.get("params", {})
        max_dur = int(self.s.get("max_fault_duration_s", 180))
        if params.get("duration_s", 0) > max_dur and action not in (
                AT.LOAD_VARIATION.value, AT.CONTROLLED_REDEPLOYMENT.value):
            reasons.append(f"duration {params.get('duration_s')}s exceeds cap {max_dur}s")
        if action == AT.POD_TERMINATION.value and \
                params.get("evict_count", 1) > int(self.s.get("max_affected_pods", 1)):
            reasons.append("blast radius exceeds max_affected_pods")
        ok = not reasons
        self.decisions.append({"ts": time.time(), "candidate": cand,
                               "allowed": ok, "reasons": reasons})
        return ok, "; ".join(reasons)

    def filter_candidates(self, candidates: list[dict], graph) -> list[dict]:
        out = []
        for c in candidates:
            ok, _ = self.validate_candidate(c, graph)
            if ok:
                out.append(c)
        return out

    # ---------- 执行期守护 ----------
    def acquire(self, experiment_id: str):
        with self._lock:
            if self._active:
                raise SafetyViolation(
                    f"concurrent experiment {self._active} still active")
            self._active = experiment_id

    def release(self, experiment_id: str):
        with self._lock:
            if self._active == experiment_id:
                self._active = None

    def check_abort(self, during_metrics: dict, baseline_metrics: dict) -> str | None:
        """执行期紧急中止条件：观测对象完全不可用且超出实验预期范围。"""
        # 错误率 100% 且吞吐归零持续 -> 可能故障扩散，提前恢复
        if during_metrics.get("span_count", 0) == 0 and \
                baseline_metrics.get("span_count", 0) > 10:
            return "observe target produced no telemetry during fault (possible outage spread)"
        return None