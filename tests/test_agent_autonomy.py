"""Agent 自主决策权扩展单元测试（全部桩 LLM/桩图谱，不依赖集群/vLLM）。

覆盖：假设验证门、候选提议规整门禁、EGV 安全集、Agent 终裁与回退、
followup 闭环、候选去重、agent 开关。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common.models import Hypothesis, Budget
from agents.hypothesis_agent import HypothesisAgent, NewHypotheses
from agents.experiment_judge import ExperimentJudge, JudgeDecision
from agents.result_analyst import ResultAnalyst, ResultExplanation
from active_verification import action_generator as ag
from active_verification.experiment_selector import ExperimentSelector


class StubLLM:
    """按 schema 名返回 canned 结构化输出。"""

    def __init__(self, responses=None, available=True):
        self.responses = responses or {}
        self._available = available
        self.tokens_used = 0

    def available(self):
        return self._available

    def complete_structured(self, system, user, schema):
        return self.responses.get(schema.__name__)


class FakeCfg:
    def __init__(self, agent=None):
        self._d = {"agent": agent if agent is not None else {"enabled": True}}
        self.system_id = "test"
        self.safety = {
            "allowed_actions": ["NETWORK_DELAY", "POD_TERMINATION", "OBSERVATION_ONLY"],
            "protected_services": ["redis-cart"],
            "max_fault_duration_s": 180,
        }

    def get(self, key, default=None):
        return self._d.get(key, default)


class StubGraph:
    def __init__(self):
        self.nodes = {"E-real": {"label": "Evidence"}}

    def services(self):
        return ["frontend", "adservice", "redis-cart"]

    def deps(self):
        return [("frontend", "adservice", {"basis": "runtime"})]

    def service_props(self, svc):
        return {}

    def latest_evidence(self, *a, **kw):
        return None

    def downstream(self, svc):
        return ["adservice"] if svc == "frontend" else []

    def subgraph_summary(self, svc):
        return {"service": svc}


def _contract(cid="RES-001", vocab=("runtime_timeout_behavior", "fallback_behavior")):
    return SimpleNamespace(id=cid, name="Timeout", dimension="resilience",
                           missing_evidence=list(vocab), actions=["NETWORK_DELAY"])


def _h(target="frontend", cid="RES-001"):
    return Hypothesis(contract_id=cid, target=target, statement="test risk",
                      missing_evidence=["runtime_timeout_behavior"])


class TestHypothesisGate(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeCfg()
        self.g = StubGraph()
        self.tools = SimpleNamespace(graph=self.g, cfg=self.cfg)
        self.agent = HypothesisAgent(StubLLM(), self.tools)
        self.contracts = {"RES-001": _contract()}

    def _validate(self, p, existing=None):
        return self.agent._validate_proposal(p, self.contracts, self.g,
                                             existing or set())

    def test_valid_proposal_accepted(self):
        h = self._validate({"contract_id": "RES-001", "target": "frontend",
                            "statement": "超时风险", "missing_evidence":
                            ["runtime_timeout_behavior"], "evidence_ids": ["E-real"],
                            "confidence": 0.8})
        self.assertIsNotNone(h)
        self.assertEqual(h.source, "agent_proposed")
        self.assertEqual(h.weight, 0.8)
        self.assertEqual(h.evidence_ids, ["E-real"])

    def test_unknown_contract_rejected(self):
        self.assertIsNone(self._validate({"contract_id": "FAKE-999",
                                          "target": "frontend", "statement": "x"}))

    def test_unknown_target_rejected(self):
        self.assertIsNone(self._validate({"contract_id": "RES-001",
                                          "target": "ghost-service", "statement": "x"}))

    def test_duplicate_rejected(self):
        self.assertIsNone(self._validate(
            {"contract_id": "RES-001", "target": "frontend", "statement": "x"},
            existing={("RES-001", "frontend")}))

    def test_invented_vocabulary_rejected(self):
        self.assertIsNone(self._validate(
            {"contract_id": "RES-001", "target": "frontend", "statement": "x",
             "missing_evidence": ["made_up_evidence"]}))

    def test_fabricated_evidence_rejected(self):
        self.assertIsNone(self._validate(
            {"contract_id": "RES-001", "target": "frontend", "statement": "x",
             "evidence_ids": ["E-fake"]}))

    def test_confidence_clamped(self):
        h = self._validate({"contract_id": "RES-001", "target": "adservice",
                            "statement": "x", "confidence": 5.0})
        self.assertEqual(h.weight, 1.0)

    def test_propose_filters_and_caps(self):
        llm = StubLLM({"NewHypotheses": {"proposals": [
            {"contract_id": "RES-001", "target": "frontend", "statement": "ok"},
            {"contract_id": "FAKE", "target": "frontend", "statement": "bad"},
            {"contract_id": "RES-001", "target": "adservice", "statement": "ok2"},
        ]}})
        agent = HypothesisAgent(llm, self.tools)
        out = agent.propose([], self.contracts)
        self.assertEqual(len(out), 2)
        self.assertEqual(agent.proposal_stats["accepted"], 2)

    def test_propose_unavailable_llm(self):
        agent = HypothesisAgent(StubLLM(available=False), self.tools)
        self.assertEqual(agent.propose([], self.contracts), [])


class TestNormalizeProposal(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeCfg()
        self.g = StubGraph()
        self.h = _h()
        self.hyps = {self.h.hypothesis_id: self.h}
        self.contracts = {"RES-001": _contract()}

    def _norm(self, prop):
        return ag.normalize_proposal(prop, self.hyps, self.contracts, self.cfg, self.g)

    def test_valid_proposal(self):
        c = self._norm({"hypothesis_id": self.h.hypothesis_id,
                        "action_type": "NETWORK_DELAY", "fault_target": "adservice",
                        "observe_target": "frontend",
                        "params": {"time_ms": 5000}, "rationale": "补超时证据"})
        self.assertIsNotNone(c)
        self.assertEqual(c["estimated_cost"], ag.ACTION_COST["NETWORK_DELAY"])
        self.assertEqual(c["params"]["time_ms"], 5000)
        self.assertEqual(c["source"], "agent")
        self.assertIn("runtime_timeout_behavior", c["expected_evidence"])
        # 单副本默认风险加成 0.20+0.15
        self.assertAlmostEqual(c["estimated_risk"], 0.35, places=3)

    def test_params_clamped(self):
        c = self._norm({"hypothesis_id": self.h.hypothesis_id,
                        "action_type": "NETWORK_DELAY", "fault_target": "adservice",
                        "observe_target": "frontend", "params": {"time_ms": 999999}})
        self.assertEqual(c["params"]["time_ms"], 10000)

    def test_illegal_action_rejected(self):
        self.assertIsNone(self._norm({"hypothesis_id": self.h.hypothesis_id,
                                      "action_type": "DROP_DATABASE",
                                      "fault_target": "adservice",
                                      "observe_target": "frontend"}))

    def test_disallowed_action_rejected(self):
        # CPU_PRESSURE 在 ACTION_PROVIDES 但不在 allowed_actions
        self.assertIsNone(self._norm({"hypothesis_id": self.h.hypothesis_id,
                                      "action_type": "CPU_PRESSURE",
                                      "fault_target": "adservice",
                                      "observe_target": "frontend"}))

    def test_protected_fault_target_rejected(self):
        self.assertIsNone(self._norm({"hypothesis_id": self.h.hypothesis_id,
                                      "action_type": "NETWORK_DELAY",
                                      "fault_target": "redis-cart",
                                      "observe_target": "frontend"}))

    def test_unknown_hypothesis_rejected(self):
        self.assertIsNone(self._norm({"hypothesis_id": "H-ghost",
                                      "action_type": "NETWORK_DELAY",
                                      "fault_target": "adservice",
                                      "observe_target": "frontend"}))

    def test_hallucinated_target_rejected(self):
        # LLM 幻觉服务名必须被图谱存在性校验拒绝（故障侧与观测侧）
        self.assertIsNone(self._norm({"hypothesis_id": self.h.hypothesis_id,
                                      "action_type": "NETWORK_DELAY",
                                      "fault_target": "cartservice-2",
                                      "observe_target": "frontend"}))
        self.assertIsNone(self._norm({"hypothesis_id": self.h.hypothesis_id,
                                      "action_type": "NETWORK_DELAY",
                                      "fault_target": "adservice",
                                      "observe_target": "ghost-observer"}))

    def test_string_param_whitelist(self):
        c = self._norm({"hypothesis_id": self.h.hypothesis_id,
                        "action_type": "NETWORK_DELAY", "fault_target": "adservice",
                        "observe_target": "frontend",
                        "params": {"interface": "eth0", "bogus_key": "x"}})
        self.assertEqual(c["params"]["interface"], "eth0")
        self.assertNotIn("bogus_key", c["params"])


class TestSafeSet(unittest.TestCase):
    def _selector(self):
        return ExperimentSelector(StubGraph(), Budget(max_wall_time_s=1000))

    def test_filters_and_caps(self):
        sel = self._selector()
        ranked = [
            {"id": "a", "utility": 0.9, "estimated_cost": 100},
            {"id": "b", "utility": 0.5, "estimated_cost": 100},
            {"id": "c", "utility": 0.0, "estimated_cost": 10},   # 非正效用剔除
            {"id": "d", "utility": -0.2, "estimated_cost": 10},
            {"id": "e", "utility": 0.8, "estimated_cost": 5000},  # 超剩余预算剔除
            {"id": "f", "utility": 0.3, "estimated_cost": 100},
        ]
        with mock.patch.object(sel, "rank", return_value=ranked):
            safe = sel.safe_set(ranked, {}, k=2)
        self.assertEqual([c["id"] for c in safe], ["a", "b"])

    def test_empty_when_no_positive(self):
        sel = self._selector()
        ranked = [{"id": "a", "utility": 0.0, "estimated_cost": 10}]
        with mock.patch.object(sel, "rank", return_value=ranked):
            self.assertEqual(sel.safe_set(ranked, {}), [])


def _cand(i, utility=0.5, cost=100, action="NETWORK_DELAY"):
    return {"hypothesis_id": "H-1", "contract_id": "RES-001", "action_type": action,
            "fault_target": f"svc{i}", "observe_target": "frontend",
            "utility": utility, "estimated_cost": cost, "estimated_risk": 0.2}


class TestJudge(unittest.TestCase):
    def setUp(self):
        self.safe = [_cand(1, 0.9), _cand(2, 0.5), _cand(3, 0.3, cost=10)]
        self.hyps = {"H-1": _h()}

    def test_valid_choice(self):
        llm = StubLLM({"JudgeDecision": {"choice": 2, "rationale": "次优但更稳"}})
        j = ExperimentJudge(llm)
        self.assertIs(j.decide(self.safe, self.hyps, {}), self.safe[1])

    def test_out_of_range_fallback_top1(self):
        llm = StubLLM({"JudgeDecision": {"choice": 99, "rationale": "x"}})
        j = ExperimentJudge(llm)
        self.assertIs(j.decide(self.safe, self.hyps, {}), self.safe[0])

    def test_decline_execution(self):
        llm = StubLLM({"JudgeDecision": {"choice": 0, "rationale": "不值得执行"}})
        j = ExperimentJudge(llm)
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(j.decide(self.safe, self.hyps, {}, run_dir=d))
            rec = json.loads((Path(d) / "agent_decisions.jsonl").read_text())
            self.assertTrue(rec["declined"])
            self.assertIsNone(rec["chosen"])

    def test_unavailable_llm_fallback(self):
        j = ExperimentJudge(StubLLM(available=False))
        self.assertIs(j.decide(self.safe, self.hyps, {}), self.safe[0])

    def test_min_cost_fallback(self):
        j = ExperimentJudge(StubLLM(available=False))
        self.assertIs(j.decide(self.safe, self.hyps, {}, fallback="min_cost"),
                      self.safe[2])

    def test_audit_written_on_fallback(self):
        j = ExperimentJudge(StubLLM(available=False))
        with tempfile.TemporaryDirectory() as d:
            j.decide(self.safe, self.hyps, {}, run_dir=d)
            rec = json.loads((Path(d) / "agent_decisions.jsonl").read_text())
            self.assertTrue(rec["fallback"])
            self.assertEqual(len(rec["safe_set"]), 3)


class TestFollowupAndDedup(unittest.TestCase):
    def test_analyst_returns_followup(self):
        llm = StubLLM({"ResultExplanation": {
            "explanation": "未定论", "next_step_suggestion": "换丢包试试",
            "followup": {"action_type": "NETWORK_LOSS", "fault_target": "adservice",
                         "observe_target": "frontend", "params": {"percent": 60},
                         "rationale": "补错误传播证据"}}})
        analyst = ResultAnalyst(llm)
        x = SimpleNamespace(experiment_id="X-1", action_type="NETWORK_DELAY",
                            fault_target="adservice", observe_target="frontend")
        obs = {"during": SimpleNamespace(metrics={}, effect_verified=False)}
        out = analyst.analyze(x, obs, {"state": "INCONCLUSIVE"}, _h())
        self.assertEqual(out["followup"]["action_type"], "NETWORK_LOSS")

    def test_followup_normalized_with_hypothesis(self):
        cfg, g, h = FakeCfg(), StubGraph(), _h()
        fu = {"action_type": "NETWORK_DELAY", "fault_target": "adservice",
              "observe_target": "frontend", "hypothesis_id": h.hypothesis_id}
        c = ag.normalize_proposal(fu, {h.hypothesis_id: h},
                                  {"RES-001": _contract()}, cfg, g)
        self.assertIsNotNone(c)
        self.assertEqual(c["hypothesis_id"], h.hypothesis_id)

    def test_dedup_static_priority(self):
        from assessment.pipeline import _dedup_candidates
        static = _cand(1) | {"source": "static"}
        agent_dup = _cand(1) | {"source": "agent"}
        agent_new = _cand(2) | {"source": "agent"}
        out = _dedup_candidates([static, agent_dup, agent_new])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["source"], "static")

    def test_agent_enabled_switch(self):
        from assessment.pipeline import _agent_enabled
        ctx = SimpleNamespace(cfg=FakeCfg(agent={"enabled": False}),
                              llm=StubLLM())
        self.assertFalse(_agent_enabled(ctx))
        ctx.cfg = FakeCfg(agent={"enabled": True})
        self.assertTrue(_agent_enabled(ctx))
        ctx.llm = StubLLM(available=False)
        self.assertFalse(_agent_enabled(ctx))


if __name__ == "__main__":
    unittest.main()