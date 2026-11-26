"""服务分类器：将探测到的 deployment 分为 critical/stateful/infra/其他，
并推断 service_mapping 和 write_operations。

LLM 可用时走结构化输出；不可用时回退纯启发式规则。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Type

from pydantic import BaseModel

from common.llm import LLMClient
from onboarding.discovery import ClusterInfo, SourceInfo


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    service_mapping: dict[str, dict]   # name -> {deployment, otel_name, source_dir}
    critical_services: list[str]
    stateful_services: list[str]
    infra_services: list[str]
    write_operations: dict[str, list[dict]]  # svc -> [{op, store, idempotent}]


# Pydantic schema for LLM structured output
class WriteOp(BaseModel):
    op: str
    store: str
    idempotent: str = "unknown"

class ServiceEntry(BaseModel):
    deployment: str
    otel_name: str
    source_dir: str | None = None

class ClassificationResult(BaseModel):
    service_mapping: dict[str, ServiceEntry]
    critical_services: list[str]
    stateful_services: list[str]
    infra_services: list[str]
    write_operations: dict[str, list[WriteOp]]


# ---------------------------------------------------------------------------
# Heuristic rules (fallback when LLM unavailable)
# ---------------------------------------------------------------------------

_CRITICAL_PATTERNS = [
    r"^frontend", r"checkout", r"^cart$", r"payment", r"product.?catalog",
    r"currency", r"shipping", r"order", r"quote", r"recommendation",
    r"^ad$", r"^email$",
]

_STATEFUL_PATTERNS = [
    r"db$", r"database", r"redis", r"cache", r"kafka", r"postgres",
    r"mysql", r"mongo", r"opensearch", r"elasticsearch", r"rabbitmq",
    r"zookeeper", r"etcd", r"valkey", r"cassandra", r"memcache",
]

_INFRA_PATTERNS = [
    r"collector", r"jaeger", r"prometheus", r"grafana", r"load.?generator",
    r"flagd", r"opamp", r"telemetry.?docs", r"^agent$", r"^mcp$",
    r"image.?provider", r"otel", r"sidecar", r"proxy",
]

_WRITE_OP_PATTERNS = [
    (r"checkout", "place_order", "kafka+payment+cart-clear"),
    (r"payment", "charge", "external-payment"),
    (r"cart", "add_item", "cache/db"),
    (r"accounting", "process_order", "kafka-consume+db"),
    (r"fraud", "detect_fraud", "kafka-consume"),
    (r"order", "create_order", "db"),
]


def _match_any(name: str, patterns: list[str]) -> bool:
    nl = name.lower()
    return any(re.search(p, nl) for p in patterns)


def _find_source_dir(deploy_name: str, source_dirs: dict) -> str | None:
    """模糊匹配 deployment name 到源码目录。"""
    # exact match
    if deploy_name in source_dirs:
        return source_dirs[deploy_name].path
    # prefix match
    for dname, info in source_dirs.items():
        if deploy_name.startswith(dname) or dname.startswith(deploy_name):
            return info.path
    # normalized match (replace - with _)
    norm = deploy_name.replace("-", "_")
    for dname, info in source_dirs.items():
        if norm == dname.replace("-", "_"):
            return info.path
    return None


def _heuristic_classify(cluster: ClusterInfo, source: SourceInfo) -> Classification:
    """纯启发式分类（LLM 不可用时的回退路径）。"""
    mapping: dict[str, dict] = {}
    critical: list[str] = []
    stateful: list[str] = []
    infra: list[str] = []
    writes: dict[str, list[dict]] = {}

    for dep in cluster.deployments:
        name = dep.name
        src_dir = _find_source_dir(name, source.service_dirs)
        mapping[name] = {
            "deployment": name,
            "otel_name": dep.otel_name,
            "source_dir": src_dir,
        }

        if _match_any(name, _CRITICAL_PATTERNS):
            critical.append(name)
        elif _match_any(name, _STATEFUL_PATTERNS):
            stateful.append(name)
        elif _match_any(name, _INFRA_PATTERNS):
            infra.append(name)

        # infer write operations
        for pattern, op, store in _WRITE_OP_PATTERNS:
            if re.search(pattern, name.lower()):
                # check if it has kafka/db dependency env vars
                env_keys = set(k.lower() for k in dep.env_vars)
                has_dep = any(x in env_keys for x in ["kafka", "db", "database", "redis"])
                if has_dep or pattern in ("checkout", "payment", "cart"):
                    writes[name] = [{"op": op, "store": store, "idempotent": "unknown"}]
                break

    return Classification(
        service_mapping=mapping,
        critical_services=sorted(critical),
        stateful_services=sorted(stateful),
        infra_services=sorted(infra),
        write_operations=writes,
    )


# ---------------------------------------------------------------------------
# LLM-enhanced classification
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM_PROMPT = """\
你是一个云原生系统架构分析专家。给定一个 K8s namespace 的 deployment 列表和源码目录结构，
请将每个服务分类为 critical（核心业务路径）、stateful（有状态存储）、infra（基础设施/可观测）或普通业务服务。
同时推断每个服务的 OTel service.name 映射和源码目录对应关系，以及可能的写操作。

规则：
- critical: 直接参与用户请求链路的业务服务（如 frontend, checkout, cart, payment）
- stateful: 数据库、缓存、消息队列等有状态组件
- infra: 可观测性工具（jaeger/prometheus/grafana）、负载生成器、配置管理等
- write_operations: 仅对有明确写入语义的服务标注（如 checkout 下单、payment 扣款）
- source_dir: 从提供的源码目录列表中匹配，无匹配则为 null
"""


def _build_discovery_summary(cluster: ClusterInfo, source: SourceInfo) -> str:
    lines = ["## Deployments"]
    for d in cluster.deployments:
        lines.append(f"- {d.name}: image={d.image.split('/')[-1] if d.image else '?'}, "
                      f"ports={d.ports}, otel_name={d.otel_name}")
        if d.env_vars:
            dep_envs = [k for k in d.env_vars if any(x in k.upper() for x in
                        ["ADDR", "URL", "HOST", "KAFKA", "DB", "REDIS"])]
            if dep_envs:
                lines.append(f"  deps: {dep_envs[:5]}")

    lines.append("\n## Source directories")
    for name, info in source.service_dirs.items():
        lines.append(f"- {name}: lang={info.language}, path={info.path}")

    lines.append(f"\n## Languages: {sorted(source.languages)}")
    lines.append(f"## Frameworks: {source.frameworks}")
    return "\n".join(lines)


def _llm_classify(cluster: ClusterInfo, source: SourceInfo,
                  llm: LLMClient) -> Classification | None:
    """尝试用 LLM 做结构化分类；失败返回 None。"""
    summary = _build_discovery_summary(cluster, source)
    result = llm.complete_structured(
        _CLASSIFY_SYSTEM_PROMPT, summary, ClassificationResult)
    if result is None:
        return None

    try:
        parsed = ClassificationResult(**result)
        mapping = {}
        for name, entry in parsed.service_mapping.items():
            mapping[name] = {
                "deployment": entry.deployment,
                "otel_name": entry.otel_name,
                "source_dir": entry.source_dir,
            }
        writes = {}
        for svc, ops in parsed.write_operations.items():
            writes[svc] = [{"op": o.op, "store": o.store,
                            "idempotent": o.idempotent} for o in ops]
        return Classification(
            service_mapping=mapping,
            critical_services=parsed.critical_services,
            stateful_services=parsed.stateful_services,
            infra_services=parsed.infra_services,
            write_operations=writes,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_services(cluster: ClusterInfo, source: SourceInfo,
                      llm: LLMClient | None = None) -> Classification:
    """分类服务。LLM 可用时优先用 LLM，失败或不可用时回退启发式。"""
    if llm and llm.available():
        result = _llm_classify(cluster, source, llm)
        if result is not None:
            return result
    return _heuristic_classify(cluster, source)
