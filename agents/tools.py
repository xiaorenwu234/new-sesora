"""Agent 工具接口（方案 §4.5 六类 Tool API，全部受限）。

权限边界：只读查询 / 只生成结构化数据 / 不直接执行。
没有通用 Shell，没有不受限 kubectl。
"""
from __future__ import annotations

from pathlib import Path

from common.config import Config
from collectors.runtime import jaeger_collector as jaeger


class AgentTools:
    def __init__(self, cfg: Config, graph, profile: dict):
        self.cfg = cfg
        self.graph = graph
        self.profile = profile
        self.instrumentation_requests: list[dict] = []   # 受审查队列
        self.verification_plans: list[dict] = []         # 提交给控制器，不执行

    # 1. 只读：跨层事实查询
    def query_evidence_graph(self, subject: str | None = None, kind: str | None = None,
                             layer: str | None = None) -> list[dict]:
        return self.graph.evidence(subject=subject, kind=kind, layer=layer)[:100]

    def subgraph(self, service: str) -> dict:
        return self.graph.subgraph_summary(service)

    # 2. 只读：源码查看（限制在受测仓库内）
    def inspect_source(self, service: str, file: str | None = None,
                       max_lines: int = 200) -> dict:
        repo = Path(self.cfg.source.repo_path).resolve()
        m = (self.profile.get("service_mapping") or {}).get(service, {})
        base = repo / (m.get("source_dir") or "")
        if file:
            target = (repo / file).resolve()
            if not str(target).startswith(str(repo)):
                return {"error": "path outside repository (denied)"}
        else:
            files = sorted(str(p.relative_to(repo)) for p in base.rglob("*")
                           if p.is_file() and p.suffix in (".py", ".go", ".js", ".java", ".cs"))
            return {"service": service, "files": files[:50]}
        try:
            lines = target.read_text(errors="replace").splitlines()[:max_lines]
            return {"file": file, "content": "\n".join(lines)}
        except Exception as e:
            return {"error": str(e)}

    # 3. 只读：运行时查询
    def query_runtime(self, service: str, lookback_s: int = 600) -> dict:
        import time
        now = time.time()
        return jaeger.window_stats(self.cfg, service, now - lookback_s, now)

    # 4. 只生成结构化数据：候选实验提议
    def propose_experiment(self, hypothesis_id: str, action_type: str,
                           fault_target: str, observe_target: str,
                           params: dict, rationale: str) -> dict:
        return {"hypothesis_id": hypothesis_id, "action_type": action_type,
                "fault_target": fault_target, "observe_target": observe_target,
                "params": params, "rationale": rationale, "status": "PROPOSED"}

    # 5. 受审查：插桩申请（不执行）
    def request_instrumentation(self, target: str, location: str, signal: str,
                                purpose: str) -> dict:
        req = {"target": target, "location": location, "signal": signal,
               "purpose": purpose, "status": "PENDING_REVIEW"}
        self.instrumentation_requests.append(req)
        return req

    # 6. 提交验证计划给控制器（不直接执行）
    def submit_verification_plan(self, plan: dict) -> dict:
        plan = dict(plan, status="SUBMITTED")
        self.verification_plans.append(plan)
        return plan