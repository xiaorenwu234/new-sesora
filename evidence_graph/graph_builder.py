"""Evidence Graph 构建器：Neo4j 持久化 + NetworkX 内存镜像。

写路径：所有节点/关系写入 Neo4j（Cypher MERGE，带 provenance），
同时在内存 NetworkX MultiDiGraph 保留完整属性镜像供图算法与查询。
Neo4j 不可达时降级为纯内存 + JSON 落盘模式（记录降级事件）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import networkx as nx

from common.config import Config
from common.models import Evidence
from evidence_graph.schema import CONSTRAINTS
from evidence_graph.entity_resolution import EntityResolver
from evidence_graph.graph_queries import GraphQueries


def _flat(props: dict) -> dict:
    """Neo4j 属性只接受标量/标量列表，复杂结构序列化为 JSON 字符串。"""
    out = {}
    for k, v in props.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float, bool)) for x in v):
            out[k] = list(v)
        else:
            out[k] = json.dumps(v, ensure_ascii=False, default=str)
    return out


class EvidenceGraph(GraphQueries):
    def __init__(self, cfg: Config, run_id: str):
        self.cfg = cfg
        self.run_id = run_id
        self.system_id = cfg.system_id
        self.nx = nx.MultiDiGraph()
        self.nodes: dict[str, dict] = {}
        self.rels: list[dict] = []
        self.degraded = False
        self._driver = None
        nc = cfg.get("neo4j", {}) or {}
        try:
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(
                nc.get("uri", "bolt://127.0.0.1:7687"),
                auth=(nc.get("user", "neo4j"), nc.get("password", "")))
            self._driver.verify_connectivity()
            with self._driver.session(database=nc.get("database", "neo4j")) as s:
                for c in CONSTRAINTS:
                    s.run(c)
        except Exception as e:
            self.degraded = True
            self.degrade_reason = str(e)
            self._driver = None

    # ---------- 基础写操作 ----------
    def add_node(self, label: str, key: str, **props):
        props = {"key": key, "label": label, "system_id": self.system_id,
                 "run_id": self.run_id, "updated_at": time.time(), **props}
        self.nodes[key] = props
        self.nx.add_node(key, **props)
        self._sync_node(label, key, props)
        return key

    def add_rel(self, src: str, rel: str, dst: str, **props):
        props = {"rel": rel, "system_id": self.system_id, "run_id": self.run_id,
                 "created_at": time.time(), **props}
        self.rels.append({"src": src, "rel": rel, "dst": dst, "props": props})
        self.nx.add_edge(src, dst, **props)
        self._sync_rel(src, rel, dst, props)

    def _sync_node(self, label, key, props):
        if not self._driver:
            return
        try:
            with self._driver.session(database=self.cfg.neo4j.get("database", "neo4j")) as s:
                s.run(f"MERGE (n:{label} {{key: $key}}) SET n += $props",
                      key=key, props=_flat(props))
        except Exception:
            self.degraded = True

    def _sync_rel(self, src, rel, dst, props):
        if not self._driver:
            return
        try:
            with self._driver.session(database=self.cfg.neo4j.get("database", "neo4j")) as s:
                s.run(
                    "MATCH (a {key: $src}), (b {key: $dst}) "
                    f"MERGE (a)-[r:{rel} {{rid: $rid}}]->(b) SET r += $props",
                    src=src, dst=dst, rid=f"{src}|{rel}|{dst}|{props.get('evidence_id', '')}",
                    props=_flat(props))
        except Exception:
            self.degraded = True

    # ---------- 证据 ingestion ----------
    def add_evidence(self, ev: Evidence):
        self.add_node("Evidence", ev.evidence_id, evidence_id=ev.evidence_id,
                      kind=ev.kind, layer=ev.layer, subject=ev.subject,
                      source=ev.source, method=ev.method, location=ev.location,
                      config_view=ev.config_view, version=ev.version,
                      collected_at=ev.collected_at, data=ev.to_dict())
        subject_key = self._subject_key(ev.subject)
        if subject_key in self.nodes:
            self.add_rel(ev.evidence_id, "ABOUT", subject_key, kind=ev.kind)

    def _subject_key(self, subject: str) -> str:
        if subject in self.nodes:
            return subject
        return subject  # Service key 即服务名

    def ingest(self, source_facts: dict, deploy_obs: dict, deploy_des: dict,
               runtime_facts: list[Evidence], profile: dict, contracts: list):
        resolver = EntityResolver(profile)
        self.resolver = resolver
        self.profile = profile
        mapping = profile.get("service_mapping") or {}

        # 1. Service 实体（显式画像）
        for svc, m in mapping.items():
            self.add_node("Service", svc, name=svc,
                          critical=svc in (profile.get("critical_services") or []),
                          stateful=svc in (profile.get("stateful_services") or []),
                          infra=svc in (profile.get("infra_services") or []),
                          otel_name=m.get("otel_name"), source_dir=m.get("source_dir"))
            if m.get("deployment"):
                dk = self.add_node("Deployment", f"deploy/{m['deployment']}",
                                   name=m["deployment"])
                self.add_rel(svc, "DEPLOYED_AS", dk, method="explicit_mapping")

        # 2. 源码实体：Function / CALLS / Config
        for fn in source_facts.get("functions", []):
            fk = f"fn/{fn['service']}/{fn['file']}/{fn['name']}"
            self.add_node("Function", fk, name=fn["name"], service=fn["service"],
                          file=fn["file"], line=fn["line"], language=fn["language"])
            self.add_rel(fn["service"], "IMPLEMENTS", fk)
        fn_by_name: dict[tuple, str] = {}
        for key, n in self.nodes.items():
            if n.get("label") == "Function":
                fn_by_name.setdefault((n["service"], n["name"]), key)
        for call in source_facts.get("calls", []):
            caller_key = fn_by_name.get((call["service"], call["caller"]))
            if not caller_key:
                continue
            callee_name = call["callee"].split(".")[-1].split("(")[0].strip()
            callee_key = fn_by_name.get((call["service"], callee_name))
            if callee_key and callee_key != caller_key:
                self.add_rel(caller_key, "CALLS", callee_key,
                             file=call["file"], line=call["line"])
        for ev in source_facts.get("evidence", []):
            self.add_evidence(ev)
            if ev.kind == "config_source":
                ck = f"config/{ev.subject}/{ev.attrs.get('env_var')}"
                self.add_node("Config", ck, name=ev.attrs.get("env_var"),
                              service=ev.subject, externalized=True)
                self.add_rel(ev.subject, "USES_CONFIG", ck, evidence_id=ev.evidence_id)

        # 3. 部署证据（双视图）+ 配置推导的静态依赖线索
        addr_re = re.compile(r"^([a-z][a-z0-9-]*):(\d{2,5})$")
        hint_seen = set()
        for ev in deploy_des.get("evidence", []) + deploy_obs.get("evidence", []):
            canonical = resolver.resolve_deployment(ev.subject)
            if canonical:
                ev.subject = canonical
            self.add_evidence(ev)
            if ev.kind == "env_configuration" and ev.subject in mapping:
                for var, val in (ev.attrs.get("env") or {}).items():
                    m = addr_re.match(str(val))
                    if not m:
                        continue
                    b = resolver.resolve_deployment(m.group(1))
                    if b and b != ev.subject and (ev.subject, b) not in hint_seen:
                        hint_seen.add((ev.subject, b))
                        self.add_rel(ev.subject, "DEPENDS_ON", b, basis="static_hint",
                                     evidence_id=ev.evidence_id, env_var=var,
                                     config_view=ev.config_view)

        # 4. 运行时证据 + 依赖边
        for ev in runtime_facts:
            self.add_evidence(ev)
            if ev.kind == "service_dependency":
                a, b = ev.subject, ev.attrs.get("downstream")
                if a in mapping and b in mapping:
                    self.add_rel(a, "DEPENDS_ON", b, basis="runtime",
                                 evidence_id=ev.evidence_id,
                                 call_count=ev.attrs.get("call_count", 0))
            elif ev.kind == "service_dependency_hint":
                hint = ev.attrs.get("downstream_hint", "")
                b = resolver.resolve_deployment(hint)
                if b and ev.subject in mapping:
                    self.add_rel(ev.subject, "DEPENDS_ON", b, basis="static_hint",
                                 evidence_id=ev.evidence_id)
            elif ev.kind == "metric_window":
                mk = f"mw/{ev.evidence_id}"
                self.add_node("MetricWindow", mk, **_flat(ev.attrs))
                self.add_rel(mk, "OBSERVES", ev.subject, evidence_id=ev.evidence_id)
            elif ev.kind == "trace_ref":
                tk = f"trace/{ev.attrs.get('trace_id')}"
                self.add_node("TraceRef", tk, trace_id=ev.attrs.get("trace_id"),
                              url=ev.location)
                self.add_rel(tk, "OBSERVES", ev.subject, evidence_id=ev.evidence_id)

        # 5. 契约实体
        for c in contracts:
            self.add_node("Contract", c.id, contract_id=c.id, name=c.name,
                          dimension=c.dimension, severity=c.severity, data=c.to_dict())

        self.alignment = resolver.alignment_report()
        return self.alignment

    # ---------- 评估对象写入 ----------
    def add_hypothesis(self, h):
        self.add_node("Hypothesis", h.hypothesis_id,
                      data=h.to_dict(), **_flat(h.to_dict()))
        self.add_rel(h.hypothesis_id, "CONCERNS", h.contract_id)
        self.add_rel(h.hypothesis_id, "TARGETS", h.target)
        for eid in h.evidence_ids:
            if eid in self.nodes:
                self.add_rel(h.hypothesis_id, "SUPPORTED_BY", eid)

    def update_hypothesis(self, h):
        self.add_node("Hypothesis", h.hypothesis_id,
                      data=h.to_dict(), **_flat(h.to_dict()))

    def add_experiment(self, x):
        self.add_node("Experiment", x.experiment_id,
                      data=x.to_dict(), **_flat(x.to_dict()))
        if x.hypothesis_id in self.nodes:
            self.add_rel(x.experiment_id, "TESTS", x.hypothesis_id)
        self.add_rel(x.experiment_id, "TARGETS", x.target)

    def update_experiment(self, x):
        self.add_node("Experiment", x.experiment_id,
                      data=x.to_dict(), **_flat(x.to_dict()))

    def add_observation(self, o):
        self.add_node("Observation", o.observation_id,
                      data=o.to_dict(), **_flat(o.to_dict()))
        if o.experiment_id in self.nodes:
            self.add_rel(o.experiment_id, "PRODUCES", o.observation_id)

    def add_finding(self, f):
        self.add_node("Finding", f.finding_id,
                      data=f.to_dict(), **_flat(f.to_dict()))
        if f.hypothesis_id and f.hypothesis_id in self.nodes:
            self.add_rel(f.finding_id, "DERIVED_FROM", f.hypothesis_id)
        for oid in f.observation_ids:
            if oid in self.nodes:
                self.add_rel(f.finding_id, "VALIDATED_BY", oid)
        for eid in f.evidence_ids:
            if eid in self.nodes:
                self.add_rel(f.finding_id, "DERIVED_FROM", eid)
        if f.contract_id in self.nodes:
            self.add_rel(f.contract_id, "APPLIES_TO", f.target)

    # ---------- 持久化 ----------
    def save(self, path: str | Path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump({"run_id": self.run_id, "system_id": self.system_id,
                       "degraded": self.degraded,
                       "alignment": getattr(self, "alignment", {}),
                       "nodes": self.nodes, "rels": self.rels},
                      f, ensure_ascii=False, default=str)

    @classmethod
    def load(cls, cfg: Config, path: str | Path) -> "EvidenceGraph":
        with open(path) as f:
            data = json.load(f)
        g = cls(cfg, data["run_id"])
        g.nodes = data["nodes"]
        g.rels = data["rels"]
        g.alignment = data.get("alignment", {})
        for key, props in g.nodes.items():
            g.nx.add_node(key, **props)
        for r in g.rels:
            g.nx.add_edge(r["src"], r["dst"], **r["props"])
        return g

    def close(self):
        if self._driver:
            self._driver.close()