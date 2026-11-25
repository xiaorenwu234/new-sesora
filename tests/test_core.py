"""核心机制单元测试（不依赖集群/Neo4j/LLM，全部用桩对象）。

覆盖论文关键不变量：
- EGV 效用分解与"不执行自由"
- 反馈状态迁移规则（确定性）
- Oracle 的 INCONCLUSIVE 语义（未生效/观测不足不算反驳）
- 安全过滤器环境保护
"""
import unittest

from common.models import Budget, Hypothesis, Observation, ContractState as CS
from active_verification.value_estimator import ValueEstimator
from active_verification.experiment_selector import ExperimentSelector
from active_verification.feedback_updater import FeedbackUpdater
from experiment_runtime import oracle_runner
from evaluation.statistical_analysis.stats import mann_whitney_u, holm_correction


class StubGraph:
    """最小图谱桩：只实现被测算路径用到的查询。"""

    def __init__(self, deps=None, runtime_subjects=()):
        self._deps = deps or {}
        self._runtime = set(runtime_subjects)
        self.evidence_added = []

    def downstream(self, svc):
        return self._deps.get(svc, [])

    def evidence(self, subject=None, kind=None, layer=None, view=None):
        if kind == "metric_window" and subject in self._runtime:
            return [{"evidence_id": "E-stub"}]
        return []

    def add_evidence(self, ev):
        self.evidence_added.append(ev)

    def update_hypothesis(self, h):
        pass


def _h(target="frontend", missing=("runtime_timeout_behavior", "upstream_slo_under_delay"),
       weight=1.0):
    return Hypothesis(contract_id="RES-001", target=target, statement="test",
                      missing_evidence=list(missing), weight=weight)


def _cand(h, action="NETWORK_DELAY", fault="productcatalogservice", provides=None):
    return {"hypothesis_id": h.hypothesis_id, "contract_id": h.contract_id,
            "action_type": action, "fault_target": fault, "observe_target": h.target,
            "provides": provides or ["runtime_timeout_behavior", "upstream_slo_under_delay"],
            "estimated_cost": 200, "estimated_risk": 0.3}


class TestEGV(unittest.TestCase):
    def test_coverage_drives_value(self):
        g = StubGraph(runtime_subjects={"frontend"})
        est = ValueEstimator(g)
        h = _h()
        full = est.p_resolve(_cand(h), h)
        partial = est.p_resolve(_cand(h, provides=["runtime_timeout_behavior"]), h)
        none = est.p_resolve(_cand(h, provides=["unrelated"]), h)
        self.assertGreater(full, partial)
        self.assertEqual(none, 0.0)

    def test_novelty_decay(self):
        est = ValueEstimator(StubGraph(runtime_subjects={"frontend"}))
        h = _h()
        p0 = est.p_resolve(_cand(h), h, attempts=0)
        p2 = est.p_resolve(_cand(h), h, attempts=2)
        self.assertAlmostEqual(p2, p0 * 0.25, places=3)

    def test_feedback_calibrates_efficacy(self):
        est = ValueEstimator(StubGraph(), history={
            "NETWORK_DELAY": {"resolved": 0, "attempts": 10}})
        self.assertLess(est.efficacy("NETWORK_DELAY"), 0.3)

    def test_no_execution_freedom(self):
        g = StubGraph(runtime_subjects={"frontend"})
        budget = Budget(lambda_cost=10.0, mu_risk=10.0)  # 成本/风险惩罚压倒价值
        sel = ExperimentSelector(g, budget)
        h = _h()
        self.assertIsNone(sel.select([_cand(h)], {h.hypothesis_id: h}))

    def test_ranking_logged(self):
        g = StubGraph(runtime_subjects={"frontend"})
        sel = ExperimentSelector(g, Budget())
        h = _h()
        best = sel.select([_cand(h)], {h.hypothesis_id: h})
        self.assertIsNotNone(best)
        self.assertIn("value_explain", best)
        self.assertEqual(len(sel.ranking_log), 1)


class TestFeedbackTransitions(unittest.TestCase):
    def _update(self, effect, sufficient, violated):
        g = StubGraph()
        fb = FeedbackUpdater(g)
        h = _h()
        obs = Observation(experiment_id="X-t", phase="during",
                          effect_verified=effect)
        verdict = {"sufficient": sufficient, "violated": violated,
                   "state": "x", "reasons": []}
        x = {"action_type": "NETWORK_DELAY", "experiment_id": "X-t",
             "fault_target": "productcatalogservice", "provides": []}
        fb.update(h, x, obs, verdict)
        return h.status

    def test_ineffective_is_inconclusive(self):
        self.assertEqual(self._update(False, True, True), CS.INCONCLUSIVE.value)

    def test_insufficient_is_inconclusive(self):
        self.assertEqual(self._update(True, False, True), CS.INCONCLUSIVE.value)

    def test_effective_violation(self):
        self.assertEqual(self._update(True, True, True), CS.VIOLATED.value)

    def test_effective_pass(self):
        self.assertEqual(self._update(True, True, False), CS.COMPLIANT.value)


class _Contract:
    oracle = {"type": "slo_violation",
              "metrics": {"error_rate": {"slo_key": "services.{target}.error_rate_max"},
                          "p99_latency": {"slo_key": "services.{target}.p99_latency_ms_max"}}}

    def oracle_thresholds(self, slo, target=None):
        return {m: None for m in self.oracle["metrics"]}


SLO = {"services": {"frontend": {"error_rate_max": 0.01, "p99_latency_ms_max": 2000}}}


def _obs(effect=True, metrics=None):
    return Observation(experiment_id="X-t", phase="during",
                       metrics=metrics or {"span_count": 100, "error_rate": 0.5,
                                           "p99_latency_ms": 3000},
                       effect_verified=effect)


class TestOracle(unittest.TestCase):
    def _run(self, obs, action="NETWORK_DELAY"):
        x = {"observe_target": "frontend", "fault_target": "productcatalogservice",
             "action_type": action}
        return oracle_runner.run_oracle(None, _Contract(), x, obs, SLO, StubGraph())

    def test_violation(self):
        v = self._run({"during": _obs()})
        self.assertEqual(v["state"], CS.VIOLATED.value)
        self.assertTrue(v["sufficient"])

    def test_metric_alias(self):
        v = self._run({"during": _obs()})
        metrics = {c["metric"] for c in v["comparisons"]}
        self.assertIn("p99_latency", metrics)  # p99_latency_ms 别名解析成功

    def test_ineffective_inconclusive(self):
        v = self._run({"during": _obs(effect=False)})
        self.assertEqual(v["state"], CS.INCONCLUSIVE.value)
        self.assertFalse(v["violated"])

    def test_low_samples_inconclusive(self):
        v = self._run({"during": _obs(metrics={"span_count": 3, "error_rate": 0.9,
                                               "p99_latency_ms": 9999})})
        self.assertEqual(v["state"], CS.INCONCLUSIVE.value)

    def test_observation_only_cannot_confirm(self):
        v = self._run({"during": _obs()}, action="OBSERVATION_ONLY")
        self.assertEqual(v["state"], CS.INCONCLUSIVE.value)


class TestStats(unittest.TestCase):
    def test_mann_whitney_separated(self):
        r = mann_whitney_u([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
        self.assertLess(r["p_value"], 0.05)

    def test_mann_whitney_identical(self):
        r = mann_whitney_u([1, 1, 1], [1, 1, 1])
        self.assertEqual(r["p_value"], 1.0)

    def test_holm_monotone(self):
        adj = holm_correction([0.01, 0.04, 0.03])
        self.assertAlmostEqual(adj[0], 0.03)
        self.assertTrue(all(a <= 1.0 for a in adj))


if __name__ == "__main__":
    unittest.main()