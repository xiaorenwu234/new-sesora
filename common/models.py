"""统一数据模型：Evidence / Hypothesis / Experiment / Observation / Finding。

设计原则（方案 §三）：Knowledge Graph 事实、Hypothesis（未验证风险）、
Finding（可报告结论）是三种独立对象，禁止互相混用。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def now() -> float:
    return time.time()


class ContractState(str, Enum):
    COMPLIANT = "COMPLIANT"
    VIOLATED = "VIOLATED"
    RISK_UNCONFIRMED = "RISK_UNCONFIRMED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"


class ActionType(str, Enum):
    POD_TERMINATION = "POD_TERMINATION"
    NETWORK_DELAY = "NETWORK_DELAY"
    NETWORK_LOSS = "NETWORK_LOSS"
    CPU_PRESSURE = "CPU_PRESSURE"
    MEMORY_PRESSURE = "MEMORY_PRESSURE"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    CONTROLLED_REDEPLOYMENT = "CONTROLLED_REDEPLOYMENT"
    LOAD_VARIATION = "LOAD_VARIATION"
    OBSERVATION_ONLY = "OBSERVATION_ONLY"
    JVM_DELAY = "JVM_DELAY"
    JVM_EXCEPTION = "JVM_EXCEPTION"
    # 只读数据深挖动作（有界多步自主探索，不注入故障）
    LOG_DEEP_DIVE = "LOG_DEEP_DIVE"
    TRACE_DRILLDOWN = "TRACE_DRILLDOWN"
    METRIC_DRILLDOWN = "METRIC_DRILLDOWN"
    CONFIG_AUDIT = "CONFIG_AUDIT"
    SOURCE_INSPECTION = "SOURCE_INSPECTION"


# 只读动作集合：不注入故障、不占用实验配额、可查受保护/基础设施服务
READONLY_ACTIONS = frozenset({
    ActionType.OBSERVATION_ONLY.value,
    ActionType.LOG_DEEP_DIVE.value,
    ActionType.TRACE_DRILLDOWN.value,
    ActionType.METRIC_DRILLDOWN.value,
    ActionType.CONFIG_AUDIT.value,
    ActionType.SOURCE_INSPECTION.value,
})


@dataclass
class Evidence:
    """一条跨层证据（图谱中的事实单元），必须带来源与采集方法。"""
    kind: str                      # 如 timeout_configuration / replica_count / service_dependency
    layer: str                     # source | deployment | runtime
    subject: str                   # 关联实体规范名（服务名/函数全名）
    attrs: dict = field(default_factory=dict)
    source: str = ""               # 采集器标识，如 semgrep / k8s_api / jaeger
    method: str = ""               # 采集方法描述
    location: str = ""             # 文件:行 / 资源名 / trace_id / promql
    config_view: str = ""          # DESIRED_CONFIG | OBSERVED_CONFIG | ""
    system_id: str = ""
    version: str = ""              # commit / image digest
    collected_at: float = field(default_factory=now)
    evidence_id: str = field(default_factory=lambda: _id("E"))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Hypothesis:
    """待验证风险：证据不足以判定契约时生成，绝不直接当作缺陷。"""
    contract_id: str
    target: str                    # 服务规范名
    statement: str
    missing_evidence: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    experiment_ids: list[str] = field(default_factory=list)  # 判定该假设的实验（含共享实验）
    status: str = ContractState.RISK_UNCONFIRMED.value
    weight: float = 1.0            # 业务重要性权重 w_h
    source: str = "deterministic"  # deterministic | agent
    system_id: str = ""
    created_at: float = field(default_factory=now)
    hypothesis_id: str = field(default_factory=lambda: _id("H"))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Experiment:
    """一次受控实验的计划与执行记录。"""
    hypothesis_id: str
    action_type: str
    target: str                    # 故障施加对象（服务）
    fault_target: str = ""         # 同 target（显式保留候选动作语义）
    observe_target: str = ""       # SLO 观测对象（上游服务）
    params: dict = field(default_factory=dict)
    workload: dict = field(default_factory=dict)
    oracle: dict = field(default_factory=dict)
    expected_evidence: list[str] = field(default_factory=list)
    estimated_cost: float = 0.0
    estimated_risk: float = 0.0
    utility: float = 0.0
    status: str = "PLANNED"        # PLANNED|RUNNING|EFFECTIVE|INEFFECTIVE|COMPLETED|ABORTED|REJECTED
    run_id: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    cost_actual: float = 0.0
    blade_name: str = ""           # ChaosBlade CR 名，用于清理
    notes: str = ""
    experiment_id: str = field(default_factory=lambda: _id("X"))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Observation:
    """实验观测结果（聚合摘要 + 原始数据引用，不存原始遥测）。"""
    experiment_id: str
    phase: str                     # baseline | during | recovery
    metrics: dict = field(default_factory=dict)   # error_rate / p99_latency_ms / throughput ...
    refs: dict = field(default_factory=dict)      # jaeger_query / promql / trace_ids / locust_stats
    effect_verified: bool = False  # 故障注入是否确认生效
    window: tuple = (0.0, 0.0)
    collected_at: float = field(default_factory=now)
    observation_id: str = field(default_factory=lambda: _id("O"))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["window"] = list(self.window)
        return d


@dataclass
class Finding:
    """最终评估结论：必须绑定契约、证据链与验证状态。"""
    contract_id: str
    target: str
    state: str                     # ContractState
    category: str                  # compliance_violation | validated_defect | unconfirmed_risk
    severity: str = "medium"
    summary: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    hypothesis_id: str = ""
    experiment_ids: list[str] = field(default_factory=list)
    observation_ids: list[str] = field(default_factory=list)
    root_cause_key: str = ""       # 去重用：contract+target+根因位置
    dimension: str = ""
    system_id: str = ""
    created_at: float = field(default_factory=now)
    finding_id: str = field(default_factory=lambda: _id("F"))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Budget:
    max_experiments: int = 8
    max_wall_time_s: float = 3600
    max_tokens: int = 200000
    lambda_cost: float = 0.15
    mu_risk: float = 0.30
    used_experiments: int = 0
    used_wall_time_s: float = 0.0
    used_tokens: int = 0
    _start: float = field(default_factory=now)

    def available(self) -> bool:
        return (self.used_experiments < self.max_experiments
                and (now() - self._start) + self.used_wall_time_s < self.max_wall_time_s
                and self.used_tokens < self.max_tokens)

    def consume(self, cost_s: float, tokens: int = 0, experiment: bool = True):
        if experiment:
            self.used_experiments += 1
        self.used_wall_time_s += cost_s
        self.used_tokens += tokens

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_start", None)
        return d