"""onboard 主编排：探测 → 分类 → 生成 → 执行 → 验证。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from common.config import Config
from common.llm import LLMClient
from onboarding.discovery import (discover_cluster, discover_source,
                                  allocate_nodeports)
from onboarding.classifier import classify_services
from onboarding.generator import generate_profile, generate_config, generate_expose


def run_onboard(args) -> int:
    """执行完整 onboard 流程。"""
    namespace = args.namespace
    kubeconfig = args.kubeconfig
    repo_path = args.repo_path
    dry_run = getattr(args, "dry_run", False)
    node_ip_override = getattr(args, "node_ip", None)

    # derive system_id from namespace
    system_id = namespace.replace("-", "_")
    project_root = Path(__file__).resolve().parent.parent

    print(f"[onboard] system_id={system_id} namespace={namespace}")
    print(f"[onboard] kubeconfig={kubeconfig} repo_path={repo_path}")

    # ------------------------------------------------------------------
    # Step 1: Discover cluster
    # ------------------------------------------------------------------
    print("\n[1/6] Discovering cluster topology...")
    try:
        cluster = discover_cluster(kubeconfig, namespace)
    except Exception as e:
        print(f"ERROR: Failed to discover cluster: {e}", file=sys.stderr)
        return 1

    print(f"  Found {len(cluster.deployments)} deployments, "
          f"{len(cluster.services)} services, {len(cluster.pods)} pods")
    print(f"  Jaeger: {'yes' if cluster.has_jaeger else 'no'}, "
          f"Prometheus: {'yes' if cluster.has_prometheus else 'no'}, "
          f"Grafana: {'yes' if cluster.has_grafana else 'no'}")
    print(f"  Node IP: {cluster.node_ip}")

    if cluster.jaeger_svc:
        print(f"  Jaeger svc: {cluster.jaeger_svc.name}:{cluster.jaeger_svc.port}")
    if cluster.prometheus_svc:
        print(f"  Prometheus svc: {cluster.prometheus_svc.name}:{cluster.prometheus_svc.port}")
    if cluster.frontend_svc:
        print(f"  Frontend svc: {cluster.frontend_svc.name}:{cluster.frontend_svc.port}")

    # ------------------------------------------------------------------
    # Step 2: Discover source
    # ------------------------------------------------------------------
    print("\n[2/6] Scanning source repository...")
    try:
        source = discover_source(repo_path)
    except Exception as e:
        print(f"WARNING: Source scan failed: {e}", file=sys.stderr)
        from onboarding.discovery import SourceInfo
        source = SourceInfo(languages=set(), service_dirs={}, frameworks=[])

    print(f"  Languages: {sorted(source.languages)}")
    print(f"  Service dirs: {len(source.service_dirs)}")
    print(f"  Frameworks: {source.frameworks}")

    # ------------------------------------------------------------------
    # Step 3: Classify services
    # ------------------------------------------------------------------
    print("\n[3/6] Classifying services...")
    llm = None
    config_path = getattr(args, "config", None)
    if config_path:
        try:
            cfg = Config.load(config_path)
            llm = LLMClient(cfg)
            if llm.available():
                print("  LLM available, using enhanced classification")
            else:
                print("  LLM not available, using heuristic fallback")
        except Exception:
            print("  Config load failed, using heuristic fallback")
    else:
        print("  No --config provided, using heuristic fallback")

    classification = classify_services(cluster, source, llm)
    print(f"  Critical: {len(classification.critical_services)} services")
    print(f"  Stateful: {len(classification.stateful_services)} services")
    print(f"  Infra: {len(classification.infra_services)} services")
    print(f"  Total mapping: {len(classification.service_mapping)} services")

    # ------------------------------------------------------------------
    # Step 4: Allocate NodePorts & generate files
    # ------------------------------------------------------------------
    print("\n[4/6] Generating configuration files...")

    # count how many NodePorts we need
    np_count = sum([bool(cluster.jaeger_svc), bool(cluster.prometheus_svc),
                    bool(cluster.frontend_svc)])
    if np_count == 0:
        print("  WARNING: No jaeger/prometheus/frontend services detected")
        node_ports = []
    else:
        try:
            node_ports = allocate_nodeports(kubeconfig, np_count)
            print(f"  Allocated NodePorts: {node_ports}")
        except Exception as e:
            print(f"  WARNING: NodePort allocation failed ({e}), using defaults")
            node_ports = list(range(31100, 31100 + np_count))

    node_ip = node_ip_override or cluster.node_ip or "127.0.0.1"

    # map ports to services
    port_idx = 0
    jaeger_port = prometheus_port = frontend_port = None
    if cluster.jaeger_svc and port_idx < len(node_ports):
        jaeger_port = node_ports[port_idx]; port_idx += 1
    if cluster.prometheus_svc and port_idx < len(node_ports):
        prometheus_port = node_ports[port_idx]; port_idx += 1
    if cluster.frontend_svc and port_idx < len(node_ports):
        frontend_port = node_ports[port_idx]; port_idx += 1

    # generate content
    profile_content = generate_profile(system_id, namespace, classification)
    config_content = generate_config(
        system_id, namespace, kubeconfig, repo_path, node_ip,
        jaeger_port, prometheus_port, frontend_port,
        source.languages, cluster.has_jaeger, cluster.has_prometheus)
    expose_content = generate_expose(
        system_id, namespace,
        cluster.jaeger_svc, cluster.prometheus_svc, cluster.frontend_svc,
        node_ports)

    # fix protected_services in config
    protected = sorted(set(classification.infra_services + classification.stateful_services))
    if protected:
        prot_str = ", ".join(protected)
        config_content = config_content.replace(
            "  protected_services: []  # filled by onboard runner",
            f"  protected_services: [{prot_str}]")

    # write files
    profile_path = project_root / "benchmarks" / "systems" / f"{system_id}.yaml"
    config_dir = project_root / "benchmarks" / "target_systems" / system_id
    config_file = config_dir / "config.yaml"
    expose_file = config_dir / "expose.sh"

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    profile_path.write_text(profile_content)
    config_file.write_text(config_content)
    expose_file.write_text(expose_content)
    expose_file.chmod(0o755)

    print(f"  Profile: {profile_path}")
    print(f"  Config:  {config_file}")
    print(f"  Expose:  {expose_file}")

    if dry_run:
        print("\n[dry-run] Files generated but NOT executed. Done.")
        return 0

    # ------------------------------------------------------------------
    # Step 5: Execute expose.sh
    # ------------------------------------------------------------------
    print("\n[5/6] Executing expose.sh (creating NodePorts + exporting manifests)...")
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    try:
        result = subprocess.run(
            ["bash", str(expose_file)],
            capture_output=True, text=True, env=env, timeout=120)
        print(result.stdout.strip())
        if result.returncode != 0:
            print(f"WARNING: expose.sh exited with code {result.returncode}",
                  file=sys.stderr)
            if result.stderr:
                print(result.stderr[:500], file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("WARNING: expose.sh timed out after 120s", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: expose.sh failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Step 6: Verify with collect
    # ------------------------------------------------------------------
    print("\n[6/6] Running collection verification...")
    try:
        from assessment.pipeline import create_context, step_collect
        cfg = Config.load(str(config_file))
        ctx = create_context(cfg, profile_path=str(profile_path),
                             run_id=f"{system_id}-onboard")
        meta = step_collect(ctx)
        alignment = meta.get("alignment", {})
        rr = alignment.get("resolution_rate", 0)
        ja = meta.get("jaeger_available", False)
        pa = meta.get("prometheus_available", False)
        print(f"  resolution_rate: {rr:.3f}")
        print(f"  jaeger_available: {ja}")
        print(f"  prometheus_available: {pa}")
        print(f"  Run dir: {ctx.run_dir}")

        if rr < 0.9:
            print("\nWARNING: resolution_rate < 0.9 — check service_mapping alignment")
        else:
            print("\nOnboard complete! Ready for assessment:")
            print(f"  python cli.py --config {config_file} assess "
                  f"--profile {profile_path}")
    except Exception as e:
        print(f"WARNING: Verification failed: {e}", file=sys.stderr)
        print("Files were generated successfully. Fix issues and retry collect manually.")

    return 0
