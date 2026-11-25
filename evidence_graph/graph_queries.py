"""Evidence Graph 查询接口（Agent 只读工具与确定性分析器共用）。"""
from __future__ import annotations

import json

import networkx as nx


def _data(node: dict) -> dict:
    d = node.get("data")
    if isinstance(d, str):
        try:
            return json.loads(d)
        except json.JSONDecodeError:
            return {}
    return d or {}


class GraphQueries:
    """mixin：依赖 self.nodes / self.rels / self.nx（由 EvidenceGraph 提供）。"""

    def services(self) -> list[str]:
        return sorted(k for k, n in self.nodes.items() if n.get("label") == "Service")

    def service_props(self, svc: str) -> dict:
        return self.nodes.get(svc, {})

    def evidence(self, subject: str | None = None, kind: str | None = None,
                 layer: str | None = None, view: str | None = None) -> list[dict]:
        out = []
        for n in self.nodes.values():
            if n.get("label") != "Evidence":
                continue
            if subject and n.get("subject") != subject:
                continue
            if kind and n.get("kind") != kind:
                continue
            if layer and n.get("layer") != layer:
                continue
            if view and n.get("config_view") != view:
                continue
            out.append(_data(n))
        return out

    def has_evidence(self, subject: str, kind: str, layer: str | None = None) -> bool:
        return bool(self.evidence(subject=subject, kind=kind, layer=layer))

    def latest_evidence(self, subject: str, kind: str, view: str | None = None,
                        layer: str | None = None) -> dict | None:
        evs = self.evidence(subject=subject, kind=kind, view=view, layer=layer)
        if not evs:
            return None
        return max(evs, key=lambda e: e.get("collected_at", 0))

    def deps(self, basis: str | None = None) -> list[tuple]:
        out = []
        for r in self.rels:
            if r["rel"] != "DEPENDS_ON":
                continue
            if basis and r["props"].get("basis") != basis:
                continue
            out.append((r["src"], r["dst"], r["props"]))
        return out

    def downstream(self, svc: str) -> list[str]:
        return sorted({b for a, b, _ in self.deps() if a == svc})

    def upstream(self, svc: str) -> list[str]:
        return sorted({a for a, b, _ in self.deps() if b == svc})

    def service_dependency_graph(self, prefer_runtime: bool = True) -> nx.DiGraph:
        """服务级依赖图：运行时观察优先，静态线索补充（分开标记 basis）。"""
        g = nx.DiGraph()
        for svc in self.services():
            g.add_node(svc)
        for a, b, p in self.deps():
            if g.has_edge(a, b):
                if p.get("basis") == "runtime":
                    g[a][b]["basis"] = "runtime"
            else:
                g.add_edge(a, b, basis=p.get("basis", "unknown"))
        if not prefer_runtime:
            return g
        return g

    def service_tags(self, svc: str) -> set[str]:
        """契约 applicability 标签（由图谱事实推导）。"""
        p = self.service_props(svc)
        tags = {"deployed_service"}
        if p.get("critical"):
            tags.add("critical_service")
        if not p.get("stateful") and not p.get("infra"):
            tags.add("stateless_expected")
        if self.downstream(svc):
            tags.add("downstream_dependency")
        if self.has_evidence(svc, "retry_policy", layer="source"):
            tags.add("retry_policy_present")
        # 写操作：显式画像标注 + 源码线索
        prof = getattr(self, "profile", None)
        if prof and svc in (prof.get("write_operations") or {}):
            tags.add("write_operations")
        if self.has_evidence(svc, "business_write"):
            tags.add("write_operations")
        return tags

    def subgraph_summary(self, svc: str) -> dict:
        """Agent 用的紧凑跨层子图摘要（不倾倒原始遥测）。"""
        kinds: dict[str, int] = {}
        for e in self.evidence(subject=svc):
            key = f"{e.get('layer')}:{e.get('kind')}"
            kinds[key] = kinds.get(key, 0) + 1
        deploy = {}
        for kind in ("replica_count", "resource_configuration", "probe_configuration",
                     "hpa_configuration", "update_strategy", "termination_configuration"):
            ev = self.latest_evidence(svc, kind, view="OBSERVED_CONFIG")
            if ev:
                deploy[kind] = ev.get("attrs")
        mw = self.latest_evidence(svc, "metric_window", layer="runtime")
        return {"service": svc,
                "tags": sorted(self.service_tags(svc)),
                "upstream": self.upstream(svc),
                "downstream": self.downstream(svc),
                "evidence_kinds": kinds,
                "observed_deployment": deploy,
                "runtime_window": (mw or {}).get("attrs", {})}

    def hypotheses(self, status: str | None = None) -> list[dict]:
        out = []
        for n in self.nodes.values():
            if n.get("label") != "Hypothesis":
                continue
            d = _data(n) or n
            if status and d.get("status") != status:
                continue
            out.append(d)
        return out

    def experiments(self) -> list[dict]:
        return [_data(n) or n for n in self.nodes.values()
                if n.get("label") == "Experiment"]

    def observations(self, experiment_id: str | None = None) -> list[dict]:
        out = []
        for n in self.nodes.values():
            if n.get("label") != "Observation":
                continue
            d = _data(n) or n
            if experiment_id and d.get("experiment_id") != experiment_id:
                continue
            out.append(d)
        return out

    def findings(self) -> list[dict]:
        return [_data(n) or n for n in self.nodes.values()
                if n.get("label") == "Finding"]

    def cypher(self, query: str, **params) -> list[dict]:
        """只读 Cypher 直通（Agent query_evidence_graph 工具底层）。"""
        if not getattr(self, "_driver", None):
            raise RuntimeError("Neo4j 不可用（降级模式），请使用结构化查询接口")
        with self._driver.session(database=self.cfg.neo4j.get("database", "neo4j")) as s:
            return [r.data() for r in s.run(query, **params)]