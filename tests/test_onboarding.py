"""onboarding 模块单元测试。

覆盖：源码探测、启发式分类、三件套生成、NodePort 分配。
集群探测用 mock（不依赖真实 k8s）。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from onboarding.discovery import (
    discover_source, allocate_nodeports,
    ClusterInfo, DeploymentInfo, ServiceInfo, PodInfo, ServiceMatch,
    _detect_language,
)
from onboarding.classifier import (
    classify_services, _heuristic_classify, Classification,
    _match_any, _CRITICAL_PATTERNS, _STATEFUL_PATTERNS, _INFRA_PATTERNS,
)
from onboarding.generator import generate_profile, generate_config, generate_expose


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_cluster(deploy_names=None, has_jaeger=True, has_prometheus=True):
    """构造测试用 ClusterInfo。"""
    if deploy_names is None:
        deploy_names = ["frontend", "checkout", "cart", "payment",
                        "product-catalog", "astronomy-db", "kafka",
                        "jaeger", "prometheus", "load-generator"]
    deploys = []
    for name in deploy_names:
        deploys.append(DeploymentInfo(
            name=name, labels={"app": name},
            image=f"ghcr.io/demo:3.0.0-{name}", replicas=1,
            ports=[8080], env_vars={}, otel_name=name,
        ))
    svcs = []
    for name in deploy_names:
        port = 16686 if name == "jaeger" else 9090 if name == "prometheus" else 8080
        svcs.append(ServiceInfo(
            name=name, svc_type="ClusterIP",
            selector={"app": name},
            ports=[{"port": port, "targetPort": port, "nodePort": None, "name": "http"}],
        ))
    jaeger_svc = ServiceMatch("jaeger", 16686, 16686, {"app": "jaeger"}) if has_jaeger else None
    prometheus_svc = ServiceMatch("prometheus", 9090, 9090, {"app": "prometheus"}) if has_prometheus else None
    frontend_svc = ServiceMatch("frontend", 8080, 8080, {"app": "frontend"})
    return ClusterInfo(
        deployments=deploys, services=svcs, pods=[],
        node_ip="192.168.5.51",
        has_jaeger=has_jaeger, has_prometheus=has_prometheus,
        jaeger_svc=jaeger_svc, prometheus_svc=prometheus_svc,
        frontend_svc=frontend_svc,
    )


def _make_source():
    """构造测试用 SourceInfo。"""
    from onboarding.discovery import SourceInfo, SourceDirInfo
    dirs = {}
    for name in ["frontend", "checkout", "cart", "payment", "product-catalog"]:
        dirs[name] = SourceDirInfo(
            path=f"src/{name}", language="go",
            has_dockerfile=True, has_proto=False,
        )
    return SourceInfo(
        languages={"go", "python", "java"},
        service_dirs=dirs,
        frameworks=["go", "nodejs"],
    )


# ---------------------------------------------------------------------------
# Source discovery tests
# ---------------------------------------------------------------------------

class TestSourceDiscovery:
    def test_detect_language_go(self, tmp_path):
        (tmp_path / "main.go").write_text("package main")
        (tmp_path / "util.go").write_text("package util")
        assert _detect_language(tmp_path) == "go"

    def test_detect_language_python(self, tmp_path):
        (tmp_path / "app.py").write_text("print('hi')")
        assert _detect_language(tmp_path) == "python"

    def test_detect_language_empty(self, tmp_path):
        assert _detect_language(tmp_path) is None

    def test_discover_source_real_repo(self):
        """对 example/opentelemetry-demo 实测（如果存在）。"""
        repo = Path("/home/xueht/new-sesora/example/opentelemetry-demo")
        if not repo.exists():
            pytest.skip("example repo not available")
        info = discover_source(str(repo))
        assert len(info.service_dirs) > 0
        assert len(info.languages) > 0
        # otel demo should have go, python, java at minimum
        assert "go" in info.languages or "python" in info.languages


# ---------------------------------------------------------------------------
# Classifier tests
# ---------------------------------------------------------------------------

class TestClassifier:
    def test_heuristic_critical(self):
        cluster = _make_cluster()
        source = _make_source()
        result = _heuristic_classify(cluster, source)
        assert "frontend" in result.critical_services
        assert "checkout" in result.critical_services
        assert "cart" in result.critical_services
        assert "payment" in result.critical_services

    def test_heuristic_stateful(self):
        cluster = _make_cluster()
        source = _make_source()
        result = _heuristic_classify(cluster, source)
        assert "astronomy-db" in result.stateful_services
        assert "kafka" in result.stateful_services

    def test_heuristic_infra(self):
        cluster = _make_cluster()
        source = _make_source()
        result = _heuristic_classify(cluster, source)
        assert "jaeger" in result.infra_services
        assert "prometheus" in result.infra_services
        assert "load-generator" in result.infra_services

    def test_service_mapping_complete(self):
        cluster = _make_cluster()
        source = _make_source()
        result = _heuristic_classify(cluster, source)
        # all deployments should be in mapping
        for dep in cluster.deployments:
            assert dep.name in result.service_mapping

    def test_source_dir_matching(self):
        cluster = _make_cluster()
        source = _make_source()
        result = _heuristic_classify(cluster, source)
        # frontend should match src/frontend
        assert result.service_mapping["frontend"]["source_dir"] == "src/frontend"
        # jaeger has no source dir
        assert result.service_mapping["jaeger"]["source_dir"] is None

    def test_classify_no_llm_fallback(self):
        """LLM=None 时应走启发式回退。"""
        cluster = _make_cluster()
        source = _make_source()
        result = classify_services(cluster, source, llm=None)
        assert isinstance(result, Classification)
        assert len(result.service_mapping) > 0

    def test_write_operations_checkout(self):
        cluster = _make_cluster()
        source = _make_source()
        result = _heuristic_classify(cluster, source)
        assert "checkout" in result.write_operations


# ---------------------------------------------------------------------------
# Generator tests
# ---------------------------------------------------------------------------

class TestGenerator:
    def test_generate_profile_valid_yaml(self):
        cluster = _make_cluster()
        source = _make_source()
        classification = _heuristic_classify(cluster, source)
        content = generate_profile("test_system", "test-ns", classification)
        assert "system_id: test_system" in content
        assert "namespace: test-ns" in content
        assert "service_mapping:" in content
        assert "critical_services:" in content
        # validate YAML syntax
        import yaml
        parsed = yaml.safe_load(content)
        assert parsed["system_id"] == "test_system"

    def test_generate_config_valid_yaml(self):
        content = generate_config(
            "test_system", "test-ns", "/path/to/kc", "/path/to/repo",
            "192.168.5.51", 31687, 31091, 31218,
            {"go", "python"}, True, True)
        assert "system_id: test_system" in content
        assert "namespace: test-ns" in content
        assert "31687" in content
        import yaml
        parsed = yaml.safe_load(content)
        assert parsed["runtime"]["jaeger_url"] == "http://192.168.5.51:31687/jaeger/ui"

    def test_generate_expose_valid_shell(self):
        jaeger_svc = ServiceMatch("jaeger", 16686, 16686, {"app": "jaeger"})
        prom_svc = ServiceMatch("prometheus", 9090, 9090, {"app": "prometheus"})
        fe_svc = ServiceMatch("frontend", 8080, 8080, {"app": "frontend"})
        content = generate_expose(
            "test_system", "test-ns",
            jaeger_svc, prom_svc, fe_svc,
            [31100, 31101, 31102])
        assert "#!/usr/bin/env bash" in content
        assert "kubectl apply" in content
        assert "31100" in content
        assert "31101" in content
        assert "31102" in content
        assert "NodePort" in content

    def test_generate_expose_no_services(self):
        content = generate_expose("test", "ns", None, None, None, [])
        assert "#!/usr/bin/env bash" in content
        # should still have manifest export
        assert "kubectl get deploy" in content


# ---------------------------------------------------------------------------
# NodePort allocation tests
# ---------------------------------------------------------------------------

class TestNodePortAllocation:
    @patch("onboarding.discovery.client")
    @patch("onboarding.discovery.k8s_config")
    def test_allocate_basic(self, mock_k8s_cfg, mock_client):
        """基本分配应返回指定数量的端口。"""
        core_mock = MagicMock()
        mock_client.CoreV1Api.return_value = core_mock
        # no existing nodeports
        ns_item = MagicMock()
        ns_item.metadata.name = "default"
        core_mock.list_namespace.return_value.items = [ns_item]
        svc_item = MagicMock()
        svc_item.spec.ports = []
        core_mock.list_namespaced_service.return_value.items = [svc_item]

        ports = allocate_nodeports("/fake/kc", count=3, start=31200)
        assert len(ports) == 3
        assert ports == [31200, 31201, 31202]

    @patch("onboarding.discovery.client")
    @patch("onboarding.discovery.k8s_config")
    def test_allocate_skip_used(self, mock_k8s_cfg, mock_client):
        """应跳过已占用的端口。"""
        core_mock = MagicMock()
        mock_client.CoreV1Api.return_value = core_mock
        ns_item = MagicMock()
        ns_item.metadata.name = "default"
        core_mock.list_namespace.return_value.items = [ns_item]
        # port 31200 is used
        used_port = MagicMock()
        used_port.node_port = 31200
        svc_item = MagicMock()
        svc_item.spec.ports = [used_port]
        core_mock.list_namespaced_service.return_value.items = [svc_item]

        ports = allocate_nodeports("/fake/kc", count=2, start=31200)
        assert 31200 not in ports
        assert len(ports) == 2


# ---------------------------------------------------------------------------
# Pattern matching tests
# ---------------------------------------------------------------------------

class TestPatterns:
    def test_critical_patterns(self):
        assert _match_any("frontend", _CRITICAL_PATTERNS)
        assert _match_any("checkout", _CRITICAL_PATTERNS)
        assert _match_any("paymentservice", _CRITICAL_PATTERNS)
        assert not _match_any("jaeger", _CRITICAL_PATTERNS)

    def test_stateful_patterns(self):
        assert _match_any("astronomy-db", _STATEFUL_PATTERNS)
        assert _match_any("redis-cart", _STATEFUL_PATTERNS)
        assert _match_any("kafka", _STATEFUL_PATTERNS)
        assert not _match_any("frontend", _STATEFUL_PATTERNS)

    def test_infra_patterns(self):
        assert _match_any("jaeger", _INFRA_PATTERNS)
        assert _match_any("otel-collector", _INFRA_PATTERNS)
        assert _match_any("load-generator", _INFRA_PATTERNS)
        assert not _match_any("checkout", _INFRA_PATTERNS)
