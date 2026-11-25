"""扩展模块单元测试（RQ3 效率指标 / 受控 mutation / JVM 动作 / 消融矩阵 / Conftest 解析）。

不依赖集群/Neo4j/LLM/外部二进制，全部用桩对象或 mock。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from evaluation.metrics import efficiency_metrics as em
from benchmarks.mutations import runner as mut
from evaluation.ablations import ablation_runner as ab
from active_verification import action_generator as ag
from experiment_runtime import controller
from experiment_runtime.jvm_injection_adapter import JvmInjectionAdapter
from experiment_runtime.chaosblade_adapter import ChaosBladeAdapter
from analyzers import external_tools


def _rec(i, state, cid="RES-001", target="frontend", cost=100):
    return {"index": i, "experiment_id": f"X-{i}", "action": "NETWORK_DELAY",
            "fault_target": "productcatalogservice", "observe_target": target,
            "contract_id": cid, "state": state, "cost_s": cost, "utility": 0.5}


class TestEfficiencyMetrics(unittest.TestCase):
    def test_dedup_same_defect(self):
        recs = [_rec(0, "VIOLATED"), _rec(1, "VIOLATED"), _rec(2, "COMPLIANT")]
        m = em.efficiency_metrics(recs)
        self.assertEqual(m["n_violated"], 2)
        self.assertEqual(m["n_distinct_defects"], 1)  # 同 (contract, target) 去重

    def test_distinct_targets_count_separately(self):
        recs = [_rec(0, "VIOLATED"), _rec(1, "VIOLATED", target="checkoutservice")]
        self.assertEqual(em.efficiency_metrics(recs)["n_distinct_defects"], 2)

    def test_auc_early_beats_late(self):
        early = em.efficiency_metrics([_rec(0, "VIOLATED"), _rec(1, "COMPLIANT")])
        late = em.efficiency_metrics([_rec(0, "COMPLIANT"), _rec(1, "VIOLATED")])
        self.assertGreater(early["auc"], late["auc"])
        self.assertAlmostEqual(early["auc"], 0.75, places=3)
        self.assertAlmostEqual(late["auc"], 0.25, places=3)

    def test_no_defect_auc_zero(self):
        m = em.efficiency_metrics([_rec(0, "INCONCLUSIVE"), _rec(1, "COMPLIANT")])
        self.assertEqual(m["auc"], 0.0)
        self.assertEqual(m["inconclusive_ratio"], 0.5)
        self.assertIsNone(m["time_to_first_defect_s"])

    def test_budget_extension(self):
        # 预算 4 只用 1 次且首次即发现：剩余区间按末值延伸，AUC=0.875
        m = em.efficiency_metrics([_rec(0, "VIOLATED")], budget_n=4)
        self.assertAlmostEqual(m["auc"], 0.875, places=3)

    def test_load_records_action_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            line = {"experiment": {"experiment_id": "X-0", "action_type": "POD_TERMINATION",
                                   "observe_target": "frontend", "cost_actual": 120},
                    "verdict": {"state": "INCONCLUSIVE"}, "candidate": {}}
            (Path(d) / "experiments.jsonl").write_text(json.dumps(line) + "\n")
            recs = em.load_records(d)
            self.assertEqual(recs[0]["contract_id"], "RES-004")  # 动作回退映射
            self.assertEqual(recs[0]["cost_s"], 120)


class TestMutationOps(unittest.TestCase):
    DOC = {"spec": {"template": {"spec": {"containers": [
        {"name": "server", "readinessProbe": {"httpGet": {"path": "/"}}}]}}}}

    def test_split_path_index(self):
        parts = mut._split_path("spec.template.spec.containers[0].readinessProbe")
        self.assertEqual(parts, ["spec", "template", "spec", "containers", 0, "readinessProbe"])

    def test_set_nested(self):
        import copy
        doc = copy.deepcopy(self.DOC)
        mut._apply_op(doc, {"op": "set", "path": "spec.replicas", "value": 1})
        self.assertEqual(doc["spec"]["replicas"], 1)

    def test_delete_list_element(self):
        import copy
        doc = copy.deepcopy(self.DOC)
        mut._apply_op(doc, {"op": "delete",
                            "path": "spec.template.spec.containers[0].readinessProbe"})
        self.assertNotIn("readinessProbe", doc["spec"]["template"]["spec"]["containers"][0])

    def test_unknown_op_raises(self):
        with self.assertRaises(ValueError):
            mut._apply_op({}, {"op": "patch", "path": "a"})


class TestAblationMatrix(unittest.TestCase):
    def test_variants_complete(self):
        self.assertEqual(len(ab.VARIANTS), 11)
        for v in ("no_cost", "no_inconclusive", "v1_algorithm_only", "v5_agent_dynamic"):
            self.assertIn(v, ab.VARIANTS)

    def test_component_switches(self):
        self.assertEqual(ab._components("v1_algorithm_only"), (True, False, False))
        self.assertEqual(ab._components("v2_agent_only"), (False, True, False))
        self.assertEqual(ab._components("v3_algo_agent"), (True, True, False))
        self.assertEqual(ab._components("v4_algo_dynamic"), (True, False, True))
        self.assertEqual(ab._components("v5_agent_dynamic"), (False, True, True))
        self.assertEqual(ab._components("full"), (True, True, True))
        self.assertEqual(ab._components("no_kg"), (True, True, True))


class TestJvmAction(unittest.TestCase):
    def test_action_tables_cover_jvm(self):
        for a in ("JVM_DELAY", "JVM_EXCEPTION"):
            self.assertIn(a, ag.ACTION_PROVIDES)
            self.assertIn(a, ag.ACTION_COST)
            self.assertIn(a, ag.ACTION_RISK)

    def test_params_contain_matchers(self):
        cfg = SimpleNamespace(safety={"max_fault_duration_s": 180})
        h = SimpleNamespace(jvm_classname="com.example.AdService",
                            jvm_methodname="getAds")
        p = ag._params("JVM_DELAY", h, None, cfg)
        self.assertEqual(p["classname"], "com.example.AdService")
        self.assertEqual(p["methodname"], "getAds")
        self.assertEqual(p["time_ms"], 3000)
        pe = ag._params("JVM_EXCEPTION", h, None, cfg)
        self.assertIn("exception", pe)

    def test_verify_effect_jvm_delay(self):
        base = {"p99_latency_ms": 100}
        during = {"p99_latency_ms": 2500}
        ok, _ = controller._verify_effect("JVM_DELAY", {"time_ms": 3000},
                                          base, during, base, during, {})
        self.assertTrue(ok)
        ok, _ = controller._verify_effect("JVM_DELAY", {"time_ms": 3000},
                                          base, base, base, base, {})
        self.assertFalse(ok)

    def test_verify_effect_jvm_exception(self):
        base = {"error_rate": 0.0}
        during = {"error_rate": 0.3}
        ok, _ = controller._verify_effect("JVM_EXCEPTION", {},
                                          base, during, base, during, {})
        self.assertTrue(ok)
        ok, _ = controller._verify_effect("JVM_EXCEPTION", {},
                                          base, base, base, base, {})
        self.assertFalse(ok)

    def test_build_experiment_spec(self):
        with mock.patch.object(ChaosBladeAdapter, "__init__", lambda self, cfg: None):
            adapter = JvmInjectionAdapter(None)
        params = {"classname": "com.example.AdService", "methodname": "getAds",
                  "time_ms": 3000}
        spec = adapter.build_experiment("JVM_DELAY", "demo", {"app": "adservice"}, params)
        self.assertEqual((spec["scope"], spec["target"], spec["action"]),
                         ("pod", "jvm", "delay"))
        names = {m["name"] for m in spec["matchers"]}
        self.assertTrue({"classname", "methodname", "effect-count", "time"} <= names)
        spec = adapter.build_experiment("JVM_EXCEPTION", "demo", {"app": "adservice"}, params)
        self.assertEqual(spec["action"], "throwCustomException")

    def test_build_experiment_requires_matchers(self):
        with mock.patch.object(ChaosBladeAdapter, "__init__", lambda self, cfg: None):
            adapter = JvmInjectionAdapter(None)
        with self.assertRaises(ValueError):
            adapter.build_experiment("JVM_DELAY", "demo", {}, {})


class TestConftestParsing(unittest.TestCase):
    def test_msg_and_metadata_split(self):
        # conftest 将 deny 对象的 msg 提升为 failure.msg，其余键入 metadata
        canned = [{"filename": "frontend.yaml", "failures": [
            {"msg": "container server image has no tag",
             "metadata": {"contract": "DEP-001", "target": "frontend"}}]}]
        cfg = SimpleNamespace(system_id=" boutique",
                              kubernetes=SimpleNamespace(manifest_dir="manifests"))
        proc = SimpleNamespace(stdout=json.dumps(canned))
        with mock.patch.object(external_tools.shutil, "which", return_value="/bin/conftest"), \
                mock.patch.object(external_tools.subprocess, "run", return_value=proc):
            evs = external_tools.run_conftest(cfg, Path(__file__).parent)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].attrs["metadata"]["contract"], "DEP-001")
        self.assertEqual(evs[0].attrs["msg"], "container server image has no tag")
        self.assertEqual(evs[0].kind, "conftest_policy")


if __name__ == "__main__":
    unittest.main()