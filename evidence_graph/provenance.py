"""证据来源（Provenance）规范：每条证据必须可回溯到采集器、方法、版本与窗口。"""
from __future__ import annotations

import time


def stamp(collector: str, method: str, version: str = "",
          window: tuple | None = None) -> dict:
    p = {"collector": collector, "method": method, "version": version,
         "stamped_at": time.time()}
    if window:
        p["window_start"], p["window_end"] = window
    return p


def evidence_chain(graph, finding: dict) -> list[dict]:
    """从 Finding 回溯完整证据链（Finding -> Hypothesis/Observation -> Evidence）。"""
    chain = []
    for eid in finding.get("evidence_ids", []):
        n = graph.nodes.get(eid)
        if n:
            chain.append({"type": "evidence", "id": eid, "kind": n.get("kind"),
                          "layer": n.get("layer"), "location": n.get("location"),
                          "source": n.get("source"), "method": n.get("method")})
    for oid in finding.get("observation_ids", []):
        n = graph.nodes.get(oid)
        if n:
            chain.append({"type": "observation", "id": oid,
                          "phase": n.get("phase"), "metrics": n.get("metrics")})
    for xid in finding.get("experiment_ids", []):
        n = graph.nodes.get(xid)
        if n:
            chain.append({"type": "experiment", "id": xid,
                          "action": n.get("action_type"), "target": n.get("target"),
                          "status": n.get("status")})
    return chain