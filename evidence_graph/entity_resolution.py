"""跨层实体对齐（自研模块，方案 §4.3）。

优先级：显式映射（系统画像） > 镜像仓库/标准元数据 > 规范化名称匹配。
无法可靠对齐的关系记录为 UNRESOLVED，绝不按名称强行关联。
对齐过程输出可审计的 resolution log，用于 RQ2 的对齐正确率度量。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict


def normalize(name: str) -> str:
    return re.sub(r"[-_\s.]", "", name.lower())


@dataclass
class Resolution:
    raw: str
    layer: str                 # source | deployment | otel
    canonical: str | None
    method: str                # explicit_mapping | image_repo | normalized_name | unresolved
    confidence: float

    def to_dict(self):
        return asdict(self)


class EntityResolver:
    def __init__(self, profile: dict):
        self.profile = profile
        self.mapping = profile.get("service_mapping") or {}
        self.log: list[Resolution] = []
        self._deploy_idx = {m["deployment"]: svc for svc, m in self.mapping.items()
                            if m.get("deployment")}
        self._otel_idx = {m["otel_name"]: svc for svc, m in self.mapping.items()
                          if m.get("otel_name")}
        self._norm_idx = {normalize(svc): svc for svc in self.mapping}

    def _record(self, raw, layer, canonical, method, conf):
        self.log.append(Resolution(raw, layer, canonical, method, conf))
        return canonical

    def resolve_deployment(self, name: str) -> str | None:
        if name in self._deploy_idx:
            return self._record(name, "deployment", self._deploy_idx[name],
                                "explicit_mapping", 1.0)
        n = normalize(name)
        if n in self._norm_idx:
            return self._record(name, "deployment", self._norm_idx[n],
                                "normalized_name", 0.8)
        return self._record(name, "deployment", None, "unresolved", 0.0)

    def resolve_otel(self, service_name: str, namespace: str | None = None) -> str | None:
        if service_name in self._otel_idx:
            return self._record(service_name, "otel", self._otel_idx[service_name],
                                "explicit_mapping", 1.0)
        n = normalize(service_name)
        if n in self._norm_idx:
            return self._record(service_name, "otel", self._norm_idx[n],
                                "normalized_name", 0.8)
        return self._record(service_name, "otel", None, "unresolved", 0.0)

    def resolve_image(self, image: str) -> str | None:
        """镜像仓库名 -> 服务（microservices-demo/<svc>:tag 约定）。"""
        repo = image.split(":")[0].split("@")[0]
        tail = repo.rsplit("/", 1)[-1]
        if tail in self.mapping:
            return self._record(image, "deployment", tail, "image_repo", 0.95)
        return self.resolve_deployment(tail)

    def alignment_report(self) -> dict:
        total = len(self.log)
        resolved = sum(1 for r in self.log if r.canonical)
        by_method = {}
        for r in self.log:
            by_method[r.method] = by_method.get(r.method, 0) + 1
        return {"total": total, "resolved": resolved,
                "resolution_rate": resolved / total if total else 1.0,
                "by_method": by_method,
                "unresolved": [r.to_dict() for r in self.log if not r.canonical],
                "log": [r.to_dict() for r in self.log]}