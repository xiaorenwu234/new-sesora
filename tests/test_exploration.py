"""数据深挖动作空间扩展单元测试（全部桩 LLM/桩工具，不依赖集群/vLLM）。

覆盖：工具白名单门禁、有界多步探索循环（步数/token/非法终止）、
确定性回退、指标确定性聚合、Oracle 兼容性门禁、只读语义
（safety 豁免/风险零/EGV 豁免/error_signal 专用判定）、审计落盘。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from common.models import Experiment, Hypothesis, Observation, READONLY_ACTIONS
from active_verification import action_generator as ag
from active_verification.value_estimator import ValueEstimator
from experiment_runtime import exploration_runner as er
from experiment_runtime.exploration_tools import ExplorationTools
from experiment_runtime.safety_controller import SafetyController
from experiment_runtime import oracle_runner


class FakeCfg(dict):
    system_id = "test"

    def __init__(self, exploration=None, allowed=None):
        super().__init__(exploration=exploration or {})
        self.safety = {
            "allowed_namespaces": ["online-boutique"],
            "allowed_actions": allowed or ["NETWORK_DELAY", "LOG_DEEP_DIVE",
                                           "TRACE_DRILLDOWN", "METRIC_DRILLDOWN",
                                           "CONFIG_AUDIT", "SOURCE_INSPECTION",
                                           "OBSERVATION_ONLY"],
            "protected_services": ["redis-cart"],
            "max_fault_duration_s": 180, "max_affected_pods": 1,
        }
        self.kubernetes = SimpleNamespace(namespace="online-boutique", kubeconfig="")


class StubGraph:
    def __init__(self):
        self.nodes = {}
        self.evidence_added = []

    def services(self):
        return ["frontend", "adservice", "redis-cart"]

    def add_evidence(self, ev):
        self.evidence_added.append(ev)
        self.nodes[ev.evidence_id] = {"label": "Evidence"}

    def service_props(self, svc):
        return {"critical": True} if svc == "frontend" else {}

    def latest_evidence(self, *a, **kw):
        return None

    def downstream(self, svc):
        return ["adservice"] if svc == "frontend" else []

    def evidence(self, *a, **kw):
        return []


class QueueLLM:
    """按调用顺序返回 canned ExplorationStep；每次累计 token。"""

    def __init__(self, steps=(), available=True, tok_per_call=100):
        self.steps = list(steps)
        self._avail = available
        self.tokens_used = 0
        self.tok = tok_per_call

    def available(self):
        return self._avail

    def complete_structured(self, system, user, schema):
        self.tokens_used += self.tok
        return self.steps.pop(0) if self.steps else {"done": True}


class StubTools:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool in self.responses:
            return self.responses[tool]
        return {"ok": True, "result": {"stub": tool},
                "metrics": {"error_log_count": 1}}


def _x(action="LOG_DEEP_DIVE", params=None):
    return Experiment(hypothesis_id="H-1", action_type=action, target="frontend",
                      fault_target="frontend", observe_target="frontend",
                      params=params if params is not None else {"lookback_s": 600},
                      expected_evidence=["error_signal_during_fault"])


def _contract(otype="error_signal_presence", actions=None):
    return SimpleNamespace(
        id="OBS-002", name="Error Observability", dimension="observability",
        missing_evidence=["error_signal_during_fault"],
        actions=actions or ["LOG_DEEP_DIVE"],
        oracle={"type": otype},
        oracle_thresholds=lambda slo: {"error_signal_detected": True})


class TestToolGates(unittest.TestCase):
    def setUp(self):
        self.tools = ExplorationTools(FakeCfg(), StubGraph())

    def test_unknown_tool_denied(self):
        self.assertFalse(self.tools.call("rm_rf", {})["ok"])
        self.assertFalse(self.tools.call("_k8s", {})["ok"])

    def test_bad_args_denied(self):
        self.assertFalse(self.tools.call("pod_logs", {"bogus": 1})["ok"])

    def test_k8s_kind_whitelist(self):
        self.assertFalse(self.tools.call("k8s_inspect", {"kind": "secrets"})["ok"])
        self.assertFalse(self.tools.call("k8s_inspect", {"kind": "nodes"})["ok"])

    def test_trace_id_validation(self):
        self.assertFalse(self.tools.call("trace_detail", {"trace_id": "../../etc"})["ok"])
        self.assertFalse(self.tools.call("trace_detail", {"trace_id": ""})["ok"])

    def test_promql_bounds(self):
        self.assertFalse(self.tools.call("prom_query", {"promql": ""})["ok"])
        self.assertFalse(self.tools.call("prom_query", {"promql": "x" * 501})["ok"])

    def test_pod_logs_keyword_filter_and_error_count(self):
        log = "INFO ok\nERROR timeout calling adservice\ninfo retry\nException: boom\n"
        core = SimpleNamespace(
            list_namespaced_pod=lambda ns, label_selector=None: SimpleNamespace(
                items=[SimpleNamespace(metadata=SimpleNamespace(name="p1"),
                                       status=SimpleNamespace(phase="Running"))]),
            read_namespaced_pod_log=lambda *a, **kw: log)
        self.tools._apis = (core, None, None, None)
        out = self.tools.call("pod_logs", {"service": "frontend", "keyword": "timeout"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["metrics"]["log_line_count"], 1)
        self.assertEqual(out["metrics"]["error_log_count"], 1)
        # 无 keyword 时确定性错误行计数 = 2（ERROR + Exception）
        out2 = self.tools.call("pod_logs", {"service": "frontend"})
        self.assertEqual(out2["metrics"]["error_log_count"], 2)
        self.assertEqual(len(self.tools.graph.evidence_added), 2)

    def test_source_path_escape_denied(self):
        self.tools.agent_tools = SimpleNamespace(
            inspect_source=lambda *a, **kw: {"error": "path outside repository (denied)"})
        self.assertFalse(self.tools.call(
            "source_inspect", {"service": "frontend", "file": "../../etc/passwd"})["ok"])


class TestExplorationLoop(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeCfg(exploration={"max_steps": 3, "max_tokens_per_session": 20000})
        self.g = StubGraph()
        self.c = _contract()

    def _run(self, llm, tools=None, run_dir=None, x=None):
        return er.run_exploration(self.cfg, self.g, x or _x(), self.c, llm,
                                  run_dir=run_dir, tools=tools or StubTools())

    def test_step_cap(self):
        llm = QueueLLM([{"tool": "pod_logs", "args": {"service": "frontend"}}] * 10)
        tools = StubTools()
        m = self._run(llm, tools)
        self.assertEqual(m["exploration_steps"], 3)
        self.assertEqual(len(tools.calls), 3)

    def test_done_early_stop(self):
        llm = QueueLLM([{"tool": "pod_logs", "args": {"service": "frontend"}},
                        {"done": True, "reason": "证据已足"}])
        m = self._run(llm)
        self.assertEqual(m["exploration_steps"], 1)

    def test_token_budget_stop(self):
        llm = QueueLLM([{"tool": "pod_logs", "args": {"service": "frontend"}}] * 10,
                       tok_per_call=30000)
        m = self._run(llm)
        self.assertEqual(m["exploration_steps"], 1)

    def test_consecutive_invalid_stop(self):
        llm = QueueLLM([{"tool": "hack", "args": {}}, {"tool": "hack2", "args": {}}])
        tools = StubTools()
        m = self._run(llm, tools)
        self.assertEqual(m["exploration_steps"], 2)
        self.assertEqual(len(tools.calls), 0)   # 白名单外不进工具

    def test_single_invalid_continues(self):
        llm = QueueLLM([{"tool": "hack", "args": {}},
                        {"tool": "pod_logs", "args": {"service": "frontend"}},
                        {"done": True}])
        m = self._run(llm)
        self.assertEqual(m["exploration_steps"], 2)
        self.assertEqual(m["error_log_count"], 1)

    def test_metrics_aggregation_and_error_count(self):
        tools = StubTools(responses={
            "pod_logs": {"ok": True, "result": {}, "metrics": {"error_log_count": 2}},
            "trace_search": {"ok": True, "result": {},
                             "metrics": {"error_span_count": 3, "trace_count": 10,
                                         "broken_trace_ratio": 0.1}}})
        llm = QueueLLM([{"tool": "pod_logs", "args": {"service": "frontend"}},
                        {"tool": "trace_search", "args": {"service": "frontend"}},
                        {"done": True}])
        m = self._run(llm, tools)
        self.assertEqual(m["error_log_count"], 2)
        self.assertEqual(m["error_span_count"], 3)
        self.assertEqual(m["error_count"], 5)          # Oracle 兼容键
        self.assertEqual(m["broken_trace_ratio"], 0.1)

    def test_deterministic_fallback_log(self):
        tools = StubTools()
        m = self._run(QueueLLM(available=False), tools)
        self.assertEqual([t for t, _ in tools.calls], ["pod_logs"])
        self.assertEqual(m["exploration_steps"], 1)

    def test_deterministic_fallback_config(self):
        tools = StubTools()
        m = self._run(QueueLLM(available=False), tools, x=_x("CONFIG_AUDIT"))
        self.assertEqual([t for t, _ in tools.calls], ["k8s_inspect", "k8s_inspect"])

    def test_audit_jsonl(self):
        llm = QueueLLM([{"tool": "pod_logs", "args": {"service": "frontend"},
                         "reason": "查错误日志"}, {"done": True}])
        with tempfile.TemporaryDirectory() as d:
            x = _x()
            self._run(llm, run_dir=d, x=x)
            p = Path(d) / "explorations" / f"{x.experiment_id}.jsonl"
            lines = [json.loads(l) for l in p.read_text().splitlines()]
            self.assertEqual(lines[0]["tool"], "pod_logs")
            self.assertEqual(lines[0]["reason"], "查错误日志")
            self.assertTrue(lines[-1]["summary"])
            self.assertEqual(lines[-1]["terminated_by"], "agent_done")


class TestOracleCompatibilityGate(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeCfg()
        self.g = StubGraph()
        self.h = Hypothesis(contract_id="OBS-002", target="frontend",
                            statement="x", missing_evidence=["error_signal_during_fault"])
        self.hyps = {self.h.hypothesis_id: self.h}

    def _norm(self, contract, action="LOG_DEEP_DIVE"):
        return ag.normalize_proposal(
            {"hypothesis_id": self.h.hypothesis_id, "action_type": action,
             "fault_target": "frontend", "observe_target": "frontend"},
            self.hyps, {contract.id: contract}, self.cfg, self.g)

    def test_injection_oracle_rejects_exploration(self):
        c = _contract(otype="slo_violation")
        c.id = "RES-001"
        self.h.contract_id = "RES-001"
        self.assertIsNone(self._norm(c))

    def test_readonly_oracle_accepts_exploration(self):
        out = self._norm(_contract())
        self.assertIsNotNone(out)
        self.assertEqual(out["estimated_cost"], 40)
        self.assertEqual(out["estimated_risk"], 0.0)   # critical+单副本仍零风险
        self.assertIn("error_signal_during_fault", out["expected_evidence"])

    def test_fault_localization_not_readonly_compatible(self):
        self.assertIsNone(self._norm(_contract(otype="fault_localization")))

    def test_generate_includes_exploration_for_compatible(self):
        c = _contract(actions=["LOG_DEEP_DIVE", "NETWORK_DELAY"])
        cands = ag.generate([self.h], self.g, {c.id: c}, self.cfg)
        actions = {x["action_type"] for x in cands}
        self.assertIn("LOG_DEEP_DIVE", actions)

    def test_generate_excludes_exploration_for_incompatible(self):
        c = _contract(otype="slo_violation", actions=["LOG_DEEP_DIVE"])
        self.assertEqual(ag.generate([self.h], self.g, {c.id: c}, self.cfg), [])


class TestReadonlySemantics(unittest.TestCase):
    def test_safety_allows_protected_target_for_exploration(self):
        cfg = FakeCfg()
        sc = SafetyController(cfg)
        g = StubGraph()
        ok, why = sc.validate_candidate(
            {"action_type": "LOG_DEEP_DIVE", "fault_target": "redis-cart",
             "observe_target": "redis-cart", "params": {}}, g)
        self.assertTrue(ok, why)
        ok2, _ = sc.validate_candidate(
            {"action_type": "NETWORK_DELAY", "fault_target": "redis-cart",
             "observe_target": "frontend", "params": {}}, g)
        self.assertFalse(ok2)

    def test_value_estimator_no_fault_penalty(self):
        ve = ValueEstimator(StubGraph())
        h = Hypothesis(contract_id="OBS-002", target="frontend", statement="x",
                       missing_evidence=["error_signal_during_fault"])
        cand = {"hypothesis_id": h.hypothesis_id, "action_type": "LOG_DEEP_DIVE",
                "fault_target": "frontend", "observe_target": "frontend",
                "provides": ["error_signal_during_fault"]}
        # 无遥测证据：observability=0.4；只读动作不再乘 fault_observability 0.6
        self.assertAlmostEqual(ve.p_resolve(cand, h), 1.0 * 0.6 * 0.4, places=4)

    def _oracle(self, metrics):
        c = _contract()
        obs = {"during": Observation(experiment_id="X-1", phase="during",
                                     metrics=metrics, effect_verified=True)}
        return oracle_runner.run_oracle(
            FakeCfg(), c, {"observe_target": "frontend",
                           "action_type": "LOG_DEEP_DIVE"}, obs, {}, StubGraph())

    def test_error_signal_detected_compliant(self):
        v = self._oracle({"span_count": 25, "error_count": 3})
        self.assertEqual(v["state"], "COMPLIANT")
        self.assertTrue(v["sufficient"])

    def test_no_error_samples_inconclusive(self):
        v = self._oracle({"span_count": 25, "error_count": 0})
        self.assertEqual(v["state"], "INCONCLUSIVE")
        self.assertFalse(v["sufficient"])

    def test_readonly_actions_membership(self):
        for a in ("LOG_DEEP_DIVE", "TRACE_DRILLDOWN", "METRIC_DRILLDOWN",
                  "CONFIG_AUDIT", "SOURCE_INSPECTION", "OBSERVATION_ONLY"):
            self.assertIn(a, READONLY_ACTIONS)
        self.assertNotIn("NETWORK_DELAY", READONLY_ACTIONS)


if __name__ == "__main__":
    unittest.main()