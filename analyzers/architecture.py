"""Architecture contracts evaluated against collected facts."""
from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict

import networkx as nx
from kubernetes.utils.quantity import parse_quantity

from common.models import ContractState as State


def result(evidence=(), issues=(), *, signal=False, complete=True):
    state = (State.RISK_UNCONFIRMED if signal else State.VIOLATED) if issues else (
        State.COMPLIANT if evidence and complete else State.INCONCLUSIVE)
    return {"state": state.value, "evidence_ids": list(dict.fromkeys(
        ev["evidence_id"] for ev in evidence)),
        "detail": " | ".join(issues) if issues else (
            "no issue in collected evidence" if state == State.COMPLIANT else "insufficient evidence")}


def observed(graph, service, kind):
    return graph.evidence(subject=service, kind=kind, view="OBSERVED_CONFIG")


def runtime_edges(graph):
    return graph.evidence(kind="service_dependency", layer="runtime")


def call_graph(edges):
    graph = nx.DiGraph()
    for edge in edges:
        source, target = edge["subject"], edge["attrs"].get("downstream")
        if target and source != target:
            count = graph.get_edge_data(source, target, {}).get("calls", 0)
            graph.add_edge(source, target, calls=count + edge["attrs"].get("call_count", 0))
    return graph


def matches(edges, source=None, target=None):
    return [e for e in edges if (source is None or e["subject"] == source)
            and (target is None or e["attrs"].get("downstream") == target)]


def priorities(graph):
    return {e["subject"]: e for e in graph.evidence(kind="service_priority")}


def workload_standards(graph, contract, service):
    if service.startswith("namespace/"):
        evidence = observed(graph, service, "namespace_quotas")
        issues = ["namespace has no resource quota"] if evidence and not evidence[0]["attrs"]["quotas"] else []
        return result(evidence, issues)

    probes = observed(graph, service, "probe_configuration")
    resources = observed(graph, service, "resource_configuration")
    security = observed(graph, service, "security_context")
    volumes = observed(graph, service, "volume_configuration")
    issues = []
    for ev in probes:
        attrs = ev["attrs"]
        issues.extend(f"{attrs['container']} missing {p} probe"
                      for p in ("readiness", "liveness") if not attrs.get(p))
    for ev in resources:
        attrs = ev["attrs"]
        for field in ("requests", "limits"):
            missing = {"cpu", "memory"} - (attrs.get(field) or {}).keys()
            if missing:
                issues.append(f"{attrs['container']} missing {field}: {', '.join(sorted(missing))}")
    for ev in security:
        attrs = ev["attrs"]
        capabilities = attrs.get("capabilities") or {}
        issues.extend(f"{attrs['container']} {message}" for condition, message in (
            (attrs.get("runAsUser") == 0 or attrs.get("runAsNonRoot") is False, "allows running as root"),
            (attrs.get("privileged") is True, "runs privileged"),
            (attrs.get("allowPrivilegeEscalation") is True, "allows privilege escalation"),
            (attrs.get("readOnlyRootFilesystem") is False, "has a writable root filesystem"),
            ("ALL" in (capabilities.get("add") or []), "adds all capabilities"),
            (bool(capabilities.get("add")) and "ALL" not in (capabilities.get("drop") or []),
             "adds capabilities without dropping ALL"),
        ) if condition)
    for ev in volumes:
        if ev["attrs"].get("host_path"):
            issues.append("workload uses a hostPath volume")
    return result(probes + resources + security + volumes, issues, complete=all((probes, resources, security, volumes)))


def docker_standards(graph, contract, service):
    evidence = graph.evidence(subject=service, kind="docker_configuration")
    issues = []
    for ev in evidence:
        attrs = ev["attrs"]
        add, drop = attrs.get("CapAdd") or [], attrs.get("CapDrop") or []
        issues.extend(message for condition, message in (
            (str(attrs.get("User") or "").split(":")[0] in {"", "root", "0"}, "uses root user"),
            (attrs.get("Privileged"), "runs privileged"),
            (any(cap in add for cap in ("CAP_SYS_ADMIN", "SYS_ADMIN")), "adds SYS_ADMIN capability"),
            (not attrs.get("ReadonlyRootfs"), "has a writable root filesystem"),
            ("ALL" in add, "adds all capabilities"),
            (bool(add) and "ALL" not in drop, "adds capabilities without dropping ALL"),
        ) if condition)
    return result(evidence, issues)


def runtime_dependency_cycles(graph, contract, service):
    edges = runtime_edges(graph)
    topology = call_graph(edges)
    if service not in topology:
        return result()
    component = next(group for group in nx.strongly_connected_components(topology) if service in group)
    evidence = [e for e in edges if e["subject"] in component and e["attrs"]["downstream"] in component]
    if len(component) > 1:
        return result(evidence, [f"cycle involving {', '.join(sorted(component))}"])
    adjacent = matches(edges, source=service) + matches(edges, target=service)
    return result(adjacent, complete=False)


def _correlation(left, right, minimum):
    scores = []
    for metric in {"latency", "error_rate"} & left.keys() & right.keys():
        first, second = dict(left[metric]), dict(right[metric])
        times = sorted(first.keys() & second.keys())
        if len(times) >= minimum:
            try:
                scores.append(statistics.correlation([first[t] for t in times], [second[t] for t in times]))
            except statistics.StatisticsError:
                continue
    return max(scores, default=None)


def dependency_priority(graph, contract, service):
    edges = runtime_edges(graph)
    outgoing = matches(edges, source=service)
    priority = priorities(graph)
    params = contract.check_params
    series = {e["subject"]: e for e in graph.evidence(kind="red_timeseries")}
    def correlation_for(edge):
        source = series.get(edge["subject"])
        target = series.get(edge["attrs"]["downstream"])
        if source and target:
            return _correlation(source["attrs"], target["attrs"], params["min_points"])
        return None

    correlation_by_edge = {e["evidence_id"]: correlation_for(e) for e in edges}
    strong = {
        (e["subject"], e["attrs"]["downstream"]) for e in edges
        if e["attrs"].get("call_count", 0) >= params["strong_calls"]
        and (correlation_by_edge[e["evidence_id"]] or 0) >= params["correlation"]
    }
    evidence, violations, signals = [], [], []
    for edge in outgoing:
        dest = edge["attrs"]["downstream"]
        correlation = correlation_by_edge[edge["evidence_id"]]
        strong_edge = (service, dest) in strong
        if service in priority and dest in priority:
            caller, callee = priority[service], priority[dest]
            source_level, target_level = caller["attrs"]["priority"], callee["attrs"]["priority"]
            evidence.extend((edge, caller, callee))
            if strong_edge and target_level > source_level:
                violations.append(f"strong dependency on lower-priority {dest} (P{source_level} to P{target_level})")
            elif (correlation is not None and target_level < source_level
                  and all(target != dest for _, target in strong)):
                violations.append(f"weak-only dependency on higher-priority {dest} (P{source_level} to P{target_level})")
        if service in series and dest in series:
            source, target = series[service], series[dest]
            if correlation is not None and correlation >= params["correlation"]:
                evidence.extend((edge, source, target))
                signals.append(f"metric correlation with {dest}: {correlation:.2f}")
    return result(evidence, violations + signals, signal=not violations, complete=bool(outgoing)
                  and all(service in priority and e["attrs"]["downstream"] in priority
                          and correlation_by_edge[e["evidence_id"]] is not None for e in outgoing))


def _usage(graph):
    return graph.evidence(kind="container_usage", layer="runtime")


def monolithic_signals(graph, contract, service):
    edges = runtime_edges(graph)
    adjacent = matches(edges, source=service) + matches(edges, target=service)
    topology = call_graph(edges)
    issues, evidence = [], list(adjacent)
    if service in topology and topology.degree(service) > contract.check_params["degree"]:
        issues.append(f"service call degree {topology.degree(service)}")
    for metric in ("cpu", "memory"):
        totals, source = defaultdict(float), []
        for ev in _usage(graph):
            if ev["attrs"].get(metric):
                totals[ev["subject"]] += ev["attrs"][metric]["mean"]
                source.append(ev)
        peers = [value for name, value in totals.items() if name != service]
        if service in totals and peers:
            baseline = statistics.median(peers)
            evidence.extend(source)
            if baseline > 0 and totals[service] > contract.check_params["resource_multiplier"] * baseline:
                issues.append(f"{metric} usage is {totals[service] / baseline:.1f}x the peer median")
    return result(evidence, issues, signal=True)


def single_point(graph, contract, service):
    edges = runtime_edges(graph)
    topology = call_graph(edges)
    priority = priorities(graph).get(service)
    critical = bool(priority and priority["attrs"]["priority"] == 0)
    issues = []
    if service not in topology:
        if not critical or not edges:
            return result()
        return result(edges + [priority], ["P0 service missing from observed dependency graph"], signal=True)
    roots = [node for node in topology if topology.in_degree(node) == 0]
    if roots:
        root = object()
        topology.add_edges_from((root, node, {"calls": 0}) for node in roots)
        dominators = nx.immediate_dominators(topology, root)
        tree = nx.DiGraph((parent, node) for node, parent in dominators.items() if node != parent)
        weighted = {node: sum(data["calls"] for _, _, data in topology.in_edges(node, data=True))
                    for node in dominators if node != root}
        total = sum(weighted.values())
        if total and service in tree:
            dominated = nx.descendants(tree, service) | {service}
            critical |= sum(weighted[node] for node in dominated) / total > contract.check_params["criticality_share"]
    if not critical:
        return result(matches(edges, source=service) + matches(edges, target=service), complete=bool(roots))
    replicas = observed(graph, service, "replica_count")
    resources = observed(graph, service, "resource_configuration")
    for ev in replicas:
        for field in ("replicas", "ready_replicas"):
            count = ev["attrs"].get(field)
            if count is not None and count < contract.check_params["min_replicas"]:
                issues.append(f"{field}={count} for critical dependency")
    issues.extend(f"{e['attrs']['container']} lacks CPU or memory requests" for e in resources
                  if not {"cpu", "memory"} <= (e["attrs"].get("requests") or {}).keys())
    return result(edges + replicas + resources + ([priority] if priority else []), issues,
                  complete=bool(replicas and resources and replicas[0]["attrs"].get("ready_replicas") is not None))


def _deployments(graph):
    history = defaultdict(list)
    for ev in graph.evidence(kind="deployment_history"):
        history[ev["subject"]].append(ev)
    return history


def _co_deployment(service, peers, history, params):
    own = history[service]
    issues, evidence = [], []
    if len(own) >= params["min_deploys"]:
        for peer in sorted(peers):
            other = history[peer]
            if len(other) < params["min_deploys"]:
                continue
            left = sorted(e["attrs"]["timestamp"] for e in own)
            right = sorted(e["attrs"]["timestamp"] for e in other)
            index = matched = 0
            for timestamp in left:
                while index < len(right) and right[index] < timestamp - params["window_s"]:
                    index += 1
                if index < len(right) and right[index] <= timestamp + params["window_s"]:
                    matched += 1
                    index += 1
            overlap = matched / max(len(left), len(right))
            evidence.extend(own + other)
            if overlap >= params["overlap"]:
                issues.append(f"co-deployment with {peer}: {overlap:.0%}")
    return evidence, issues


def over_decomposition(graph, contract, service):
    edges = runtime_edges(graph)
    topology = call_graph(edges)
    if service not in topology:
        return result()
    params = contract.check_params
    condensed = nx.condensation(topology)
    path = {node: 1 for node in condensed}
    for node in reversed(list(nx.topological_sort(condensed))):
        path[node] += max((path[child] for child in condensed.successors(node)), default=0)
    depth = path[condensed.graph["mapping"][service]]
    pipes = {node for node in topology if topology.in_degree(node) == topology.out_degree(node) == 1}
    incoming = sum(e["calls"] for _, _, e in topology.in_edges(service, data=True))
    outgoing = sum(e["calls"] for _, _, e in topology.out_edges(service, data=True))
    issues = []
    if depth >= params["path_services"]:
        issues.append(f"dependency chain spans at least {depth} service components")
    if service in pipes and len(pipes) / len(topology) > params["pipe_ratio"]:
        issues.append(f"pass-through services account for {len(pipes) / len(topology):.0%} of the graph")
    if (topology.out_degree(service) > 1 and incoming >= params["min_inbound_calls"]
            and outgoing / incoming >= params["amplification"]):
        issues.append(f"fan-out amplification {outgoing / incoming:.1f}x")
    history, coupled = _co_deployment(service, set(topology.neighbors(service)), _deployments(graph), params)
    return result(edges + history, issues + coupled, signal=True)


def resource_utilization(graph, contract, service):
    resources = observed(graph, service, "resource_configuration")
    usage = {e["attrs"]["container"]: e for e in _usage(graph) if e["subject"] == service}
    params = contract.check_params
    issues, evidence = [], list(resources)
    covered = 0
    for ev in resources:
        attrs = ev["attrs"]
        container = attrs["container"]
        sample = usage.get(container)
        for metric in ("cpu", "memory"):
            request = (attrs.get("requests") or {}).get(metric)
            limit = (attrs.get("limits") or {}).get(metric)
            if not request:
                issues.append(f"{container} has no {metric} request")
            if not sample or not sample["attrs"].get(metric):
                continue
            covered += 1
            evidence.append(sample)
            stats = sample["attrs"][metric]
            if request and stats["peak"] < float(parse_quantity(request)) * params["waste_ratio"]:
                issues.append(f"{container} peak {metric} use below {params['waste_ratio']:.0%} of request")
            if limit and stats["peak"] >= float(parse_quantity(limit)) * params["limit_ratio"]:
                issues.append(f"{container} peak {metric} use reaches {params['limit_ratio']:.0%} of limit")
    return result(evidence, issues, complete=covered == 2 * len(resources))


def persistent_config_drift(graph, contract, service):
    evidence = graph.evidence(subject=service, kind="config_change")
    if not evidence:
        return result()
    latest = max(evidence, key=lambda e: e["attrs"]["timestamp"])
    attrs = latest["attrs"]
    age = (time.time() - attrs["timestamp"]) / 3600
    issues = [f"latest manual configuration write is {age:.1f} hours old"] if (
        attrs["manual"] and not attrs["rollback"] and not attrs["created"]
        and age > contract.check_params["age_hours"]) else []
    return result([latest], issues, signal=True)


def service_maintainability(graph, contract, service):
    params = contract.check_params
    pods = [e for e in observed(graph, service, "pod_status") if e["attrs"].get("startup_s") is not None]
    issues = [f"{e['attrs']['pod']} took {e['attrs']['startup_s']:.1f}s to become ready"
              for e in pods if e["attrs"]["startup_s"] > params["startup_s"]]
    history = _deployments(graph)
    peers = set(history) - {service}
    own = history[service]
    if own:
        rollback_ratio = sum(e["attrs"]["rollback"] for e in own) / len(own)
        if len(own) >= params["min_deploys"] and rollback_ratio > params["rollback_ratio"]:
            issues.append(f"rollback rate {rollback_ratio:.0%}")
    peer_history = [e for peer in peers for e in history[peer]]
    if peers and len(own) < statistics.median(len(history[peer]) for peer in peers) * params["frequency_ratio"]:
        issues.append(f"deployment frequency below peer baseline ({len(own)} rollouts)")
    paired, coupled = _co_deployment(service, peers, history, params)
    return result(pods + own + peer_history + paired, issues + coupled, signal=True,
                  complete=bool(pods and own))


def physical_isolation(graph, contract, service):
    minimum = contract.check_params["min_replicas"]
    pods = [e for e in observed(graph, service, "pod_status")
            if e["attrs"].get("phase") == "Running" and e["attrs"].get("node")]
    priority = priorities(graph)
    evidence = list(pods)
    issues = []
    if len(pods) >= minimum:
        for domain in ("node", "zone"):
            placements = [e["attrs"].get(domain) for e in pods]
            counts = Counter(place for place in placements if place)
            for name, count in sorted(counts.items()):
                if count >= minimum and (domain == "node" or count == len(pods)):
                    issues.append(f"{count} running replicas share {domain} {name}")
    if service in priority and priority[service]["attrs"]["priority"] == 0:
        nodes = {e["attrs"]["node"] for e in pods}
        for other, other_priority in priority.items():
            if other == service or other_priority["attrs"]["priority"] != 0:
                continue
            colocated = [e for e in observed(graph, other, "pod_status")
                         if e["attrs"].get("phase") == "Running" and e["attrs"].get("node") in nodes]
            if colocated:
                evidence.extend(colocated + [priority[service], other_priority])
                issues.append(f"shares a node with P0 service {other}")
    return result(evidence, issues, complete=len(pods) >= minimum and all(e["attrs"].get("zone") for e in pods))


CHECKS = {fn.__name__: fn for fn in (
    workload_standards, docker_standards, runtime_dependency_cycles, dependency_priority,
    monolithic_signals, single_point, over_decomposition, resource_utilization,
    persistent_config_drift, service_maintainability, physical_isolation,
)}

GRAPH_CHECKS = {"runtime_dependency_cycles", "dependency_priority", "monolithic_signals",
                "single_point", "over_decomposition"}


def targets(graph, contract):
    if contract.static_check == "docker_standards":
        return sorted({e["subject"] for e in graph.evidence(kind="docker_configuration")})
    services = set(graph.services())
    if contract.static_check == "workload_standards":
        services.update(e["subject"] for e in graph.evidence(kind="security_context", view="OBSERVED_CONFIG"))
        if services:
            services.update(e["subject"] for e in graph.evidence(kind="namespace_quotas"))
    if contract.static_check in {"persistent_config_drift", "service_maintainability"}:
        kinds = ("config_change",) if contract.static_check == "persistent_config_drift" else ("deployment_history",)
        services.update(e["subject"] for kind in kinds for e in graph.evidence(kind=kind))
    return sorted(services)
