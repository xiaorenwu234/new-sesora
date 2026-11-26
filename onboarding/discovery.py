"""集群与源码探测（纯确定性，无 LLM 依赖）。

从 k8s API 采集 deployment/service/pod 拓扑，从文件系统扫描源码结构。
返回结构化 dataclass 供 classifier 和 generator 消费。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from kubernetes import client, config as k8s_config


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DeploymentInfo:
    name: str
    labels: dict[str, str]
    image: str
    replicas: int
    ports: list[int]
    env_vars: dict[str, str]
    otel_name: str  # inferred OTel service.name


@dataclass
class ServiceInfo:
    name: str
    svc_type: str  # ClusterIP / NodePort / LoadBalancer
    selector: dict[str, str]
    ports: list[dict]  # [{port, targetPort, nodePort, name}]


@dataclass
class PodInfo:
    name: str
    owner: str | None
    ready: bool
    restarts: int
    node_name: str | None
    node_ip: str | None


@dataclass
class ServiceMatch:
    """识别到的基础设施/入口服务匹配结果。"""
    name: str
    port: int
    target_port: int
    selector: dict[str, str]


@dataclass
class ClusterInfo:
    deployments: list[DeploymentInfo]
    services: list[ServiceInfo]
    pods: list[PodInfo]
    node_ip: str
    has_jaeger: bool = False
    has_prometheus: bool = False
    has_grafana: bool = False
    jaeger_svc: ServiceMatch | None = None
    prometheus_svc: ServiceMatch | None = None
    frontend_svc: ServiceMatch | None = None


@dataclass
class SourceDirInfo:
    path: str
    language: str | None
    has_dockerfile: bool
    has_proto: bool


@dataclass
class SourceInfo:
    languages: set[str]
    service_dirs: dict[str, SourceDirInfo]
    frameworks: list[str]


# ---------------------------------------------------------------------------
# Language detection helpers
# ---------------------------------------------------------------------------

_EXT_LANG = {
    ".py": "python", ".go": "go", ".java": "java", ".js": "javascript",
    ".ts": "javascript", ".tsx": "javascript", ".jsx": "javascript",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".rs": "rust",
    ".cpp": "cpp", ".cc": "cpp", ".c": "cpp", ".h": "cpp", ".hpp": "cpp",
    ".kt": "kotlin", ".scala": "scala", ".swift": "swift",
}

_FRAMEWORK_MARKERS = {
    "package.json": "nodejs",
    "go.mod": "go",
    "pom.xml": "java-maven",
    "build.gradle": "java-gradle",
    "build.gradle.kts": "java-gradle-kotlin",
    "Cargo.toml": "rust",
    "Gemfile": "ruby",
    "composer.json": "php",
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "setup.py": "python",
}


def _detect_language(directory: Path) -> str | None:
    """按文件扩展名统计，返回占比最高的语言。"""
    counts: dict[str, int] = {}
    try:
        for f in directory.rglob("*"):
            if f.is_file() and f.suffix in _EXT_LANG:
                lang = _EXT_LANG[f.suffix]
                counts[lang] = counts.get(lang, 0) + 1
    except PermissionError:
        return None
    if not counts:
        return None
    return max(counts, key=counts.get)


def _detect_frameworks(directory: Path) -> list[str]:
    found = []
    for marker, fw in _FRAMEWORK_MARKERS.items():
        if (directory / marker).exists():
            found.append(fw)
    # also check for .csproj files
    try:
        if any(directory.glob("*.csproj")):
            found.append("dotnet")
    except PermissionError:
        pass
    return found


# ---------------------------------------------------------------------------
# Cluster discovery
# ---------------------------------------------------------------------------

def _infer_otel_name(deploy_name: str, pod_annotations: dict,
                     env_vars: dict) -> str:
    """推断 OTel service.name：annotation > env > deployment name。"""
    ann = pod_annotations.get("opentelemetry.io/name", "")
    if ann:
        return ann
    env_svc = env_vars.get("OTEL_SERVICE_NAME", "")
    if env_svc:
        return env_svc
    return deploy_name


def _is_jaeger(name: str, labels: dict) -> bool:
    return (labels.get("app.kubernetes.io/name") == "jaeger"
            or "jaeger" in name.lower())


def _is_prometheus(name: str, labels: dict) -> bool:
    return (labels.get("app.kubernetes.io/name") == "prometheus"
            or labels.get("name") == "prometheus"
            or "prometheus" in name.lower())


def _is_grafana(name: str, labels: dict) -> bool:
    return (labels.get("app.kubernetes.io/name") == "grafana"
            or "grafana" in name.lower())


def _is_frontend(name: str, ports: list[dict]) -> bool:
    if "frontend" not in name.lower():
        return False
    http_ports = {80, 8080, 3000, 8000, 8888}
    return any(p.get("port") in http_ports or p.get("targetPort") in http_ports
               for p in ports)


def discover_cluster(kubeconfig: str, namespace: str) -> ClusterInfo:
    """从 k8s API 采集集群拓扑信息。"""
    k8s_config.load_kube_config(config_file=kubeconfig)
    core = client.CoreV1Api()
    apps = client.AppsV1Api()

    # --- Deployments ---
    deploys: list[DeploymentInfo] = []
    for d in apps.list_namespaced_deployment(namespace).items:
        meta = d.metadata
        spec = d.spec
        tmpl = spec.template.spec
        containers = tmpl.containers or []
        c0 = containers[0] if containers else client.V1Container(name="unknown")

        env_map = {}
        for e in (c0.env or []):
            if e.value is not None:
                env_map[e.name] = e.value

        ports = [p.container_port for p in (c0.ports or []) if p.container_port]

        # infer otel name from pod template annotations
        pod_ann = (spec.template.metadata.annotations or {}) if spec.template.metadata else {}
        otel_name = _infer_otel_name(meta.name, pod_ann, env_map)

        deploys.append(DeploymentInfo(
            name=meta.name,
            labels=dict(meta.labels or {}),
            image=c0.image or "",
            replicas=spec.replicas or 1,
            ports=ports,
            env_vars=env_map,
            otel_name=otel_name,
        ))

    # Also check DaemonSets and StatefulSets
    for ds in apps.list_namespaced_daemon_set(namespace).items:
        meta = ds.metadata
        tmpl = ds.spec.template.spec
        containers = tmpl.containers or []
        c0 = containers[0] if containers else client.V1Container(name="unknown")
        env_map = {}
        for e in (c0.env or []):
            if e.value is not None:
                env_map[e.name] = e.value
        ports = [p.container_port for p in (c0.ports or []) if p.container_port]
        pod_ann = (ds.spec.template.metadata.annotations or {}) if ds.spec.template.metadata else {}
        otel_name = _infer_otel_name(meta.name, pod_ann, env_map)
        deploys.append(DeploymentInfo(
            name=meta.name, labels=dict(meta.labels or {}),
            image=c0.image or "", replicas=1, ports=ports,
            env_vars=env_map, otel_name=otel_name,
        ))

    for ss in apps.list_namespaced_stateful_set(namespace).items:
        meta = ss.metadata
        tmpl = ss.spec.template.spec
        containers = tmpl.containers or []
        c0 = containers[0] if containers else client.V1Container(name="unknown")
        env_map = {}
        for e in (c0.env or []):
            if e.value is not None:
                env_map[e.name] = e.value
        ports = [p.container_port for p in (c0.ports or []) if p.container_port]
        pod_ann = (ss.spec.template.metadata.annotations or {}) if ss.spec.template.metadata else {}
        otel_name = _infer_otel_name(meta.name, pod_ann, env_map)
        deploys.append(DeploymentInfo(
            name=meta.name, labels=dict(meta.labels or {}),
            image=c0.image or "", replicas=ss.spec.replicas or 1,
            ports=ports, env_vars=env_map, otel_name=otel_name,
        ))

    # --- Services ---
    svcs: list[ServiceInfo] = []
    for s in core.list_namespaced_service(namespace).items:
        meta = s.metadata
        spec = s.spec
        port_list = []
        for p in (spec.ports or []):
            port_list.append({
                "port": p.port,
                "targetPort": p.target_port,
                "nodePort": p.node_port,
                "name": p.name,
            })
        svcs.append(ServiceInfo(
            name=meta.name,
            svc_type=spec.type or "ClusterIP",
            selector=dict(spec.selector or {}),
            ports=port_list,
        ))

    # --- Pods ---
    pods: list[PodInfo] = []
    node_ip = ""
    for p in core.list_namespaced_pod(namespace).items:
        meta = p.metadata
        statuses = p.status.container_statuses or []
        restarts = sum(cs.restart_count or 0 for cs in statuses)
        ready = all(cs.ready for cs in statuses) if statuses else False
        owner = None
        for ref in (meta.owner_references or []):
            if ref.kind == "ReplicaSet":
                owner = ref.name.rsplit("-", 1)[0] if "-" in ref.name else ref.name
                break
            elif ref.kind in ("DaemonSet", "StatefulSet"):
                owner = ref.name
                break

        n_ip = p.status.host_ip
        if n_ip and not node_ip:
            node_ip = n_ip

        pods.append(PodInfo(
            name=meta.name, owner=owner, ready=ready,
            restarts=restarts, node_name=p.spec.node_name,
            node_ip=n_ip,
        ))

    # --- Identify infra services ---
    has_jaeger = has_prometheus = has_grafana = False
    jaeger_svc = prometheus_svc = frontend_svc = None

    for svc in svcs:
        labels = {}
        # find matching deployment labels
        for dep in deploys:
            if dep.name == svc.name or svc.name.startswith(dep.name):
                labels = dep.labels
                break

        if _is_jaeger(svc.name, labels):
            has_jaeger = True
            for p in svc.ports:
                if p.get("port") in (16686, 16685, 4317):
                    jaeger_svc = ServiceMatch(
                        name=svc.name, port=p["port"],
                        target_port=p.get("targetPort", p["port"]),
                        selector=svc.selector)
                    break
            if not jaeger_svc and svc.ports:
                p = svc.ports[0]
                jaeger_svc = ServiceMatch(
                    name=svc.name, port=p["port"],
                    target_port=p.get("targetPort", p["port"]),
                    selector=svc.selector)

        if _is_prometheus(svc.name, labels):
            has_prometheus = True
            for p in svc.ports:
                if p.get("port") == 9090:
                    prometheus_svc = ServiceMatch(
                        name=svc.name, port=9090,
                        target_port=p.get("targetPort", 9090),
                        selector=svc.selector)
                    break

        if _is_grafana(svc.name, labels):
            has_grafana = True

        if _is_frontend(svc.name, svc.ports):
            for p in svc.ports:
                tp = p.get("targetPort", p["port"])
                if tp in (80, 8080, 3000, 8000):
                    frontend_svc = ServiceMatch(
                        name=svc.name, port=p["port"],
                        target_port=tp, selector=svc.selector)
                    break

    return ClusterInfo(
        deployments=deploys, services=svcs, pods=pods,
        node_ip=node_ip,
        has_jaeger=has_jaeger, has_prometheus=has_prometheus,
        has_grafana=has_grafana,
        jaeger_svc=jaeger_svc, prometheus_svc=prometheus_svc,
        frontend_svc=frontend_svc,
    )


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------

def discover_source(repo_path: str) -> SourceInfo:
    """扫描源码仓库，检测语言、服务目录、框架。"""
    root = Path(repo_path)
    src_dir = root / "src"
    if not src_dir.exists():
        src_dir = root  # fallback: repo root IS the source root

    languages: set[str] = set()
    service_dirs: dict[str, SourceDirInfo] = {}
    frameworks: list[str] = []

    # detect top-level frameworks
    frameworks.extend(_detect_frameworks(root))

    # scan service directories under src/
    if src_dir.is_dir():
        for entry in sorted(src_dir.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            lang = _detect_language(entry)
            if lang:
                languages.add(lang)
            fw = _detect_frameworks(entry)
            frameworks.extend(fw)
            service_dirs[name] = SourceDirInfo(
                path=str(entry.relative_to(root)),
                language=lang,
                has_dockerfile=(entry / "Dockerfile").exists(),
                has_proto=any(entry.glob("**/*.proto")),
            )

    return SourceInfo(
        languages=languages,
        service_dirs=service_dirs,
        frameworks=list(set(frameworks)),
    )


# ---------------------------------------------------------------------------
# NodePort allocation
# ---------------------------------------------------------------------------

def allocate_nodeports(kubeconfig: str, count: int = 3,
                       start: int = 31000) -> list[int]:
    """分配空闲 NodePort 端口，避开已占用端口。"""
    k8s_config.load_kube_config(config_file=kubeconfig)
    core = client.CoreV1Api()

    used: set[int] = set()
    for ns in core.list_namespace().items:
        for svc in core.list_namespaced_service(ns.metadata.name).items:
            for p in (svc.spec.ports or []):
                if p.node_port:
                    used.add(p.node_port)

    allocated: list[int] = []
    port = start
    while len(allocated) < count and port <= 32767:
        if port not in used:
            allocated.append(port)
        port += 1
    return allocated
