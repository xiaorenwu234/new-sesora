"""图算法分析器：依赖循环、中心性、关键路径单点（复用 NetworkX）。"""
from __future__ import annotations

import networkx as nx


def analyze(graph) -> dict:
    dg = graph.service_dependency_graph()
    cycles = [sorted(c) for c in nx.strongly_connected_components(dg) if len(c) > 1]
    try:
        centrality = nx.betweenness_centrality(dg)
    except Exception:
        centrality = {n: 0.0 for n in dg.nodes}
    # 关键路径单点：critical 服务、副本=1、无 HPA、且存在上游
    spof = []
    for svc in dg.nodes:
        p = graph.service_props(svc)
        if not p.get("critical") or p.get("infra"):
            continue
        rep = graph.latest_evidence(svc, "replica_count", view="OBSERVED_CONFIG")
        hpa = graph.latest_evidence(svc, "hpa_configuration", view="OBSERVED_CONFIG")
        n = (rep or {}).get("attrs", {}).get("replicas", 1)
        if n <= 1 and not hpa and graph.upstream(svc):
            spof.append(svc)
    # 支配性依赖：被所有 critical 上游依赖的服务
    critical = [s for s in dg.nodes if graph.service_props(s).get("critical")]
    shared = []
    for s in dg.nodes:
        ups = set(graph.upstream(s)) & set(critical)
        if len(ups) >= 3:
            shared.append({"service": s, "critical_upstreams": sorted(ups)})
    return {"cycles": cycles, "betweenness_centrality": centrality,
            "single_points_of_failure": spof, "shared_dependencies": shared,
            "edge_count": dg.number_of_edges(), "node_count": dg.number_of_nodes()}