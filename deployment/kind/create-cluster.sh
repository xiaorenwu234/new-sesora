#!/usr/bin/env bash
# 在 kind 隔离集群中搭建完整评估环境（方案 §11）。
# 依赖：kind、podman（KIND_EXPERIMENTAL_PROVIDER=podman）、kubectl、helm。
set -euo pipefail
export KIND_EXPERIMENTAL_PROVIDER="${KIND_EXPERIMENTAL_PROVIDER:-podman}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# 1. 创建集群
kind create cluster --config "$HERE/kind-config.yaml"
kubectl cluster-info --context kind-cnaa

# 2. ChaosBlade Operator（故障注入）
helm repo add chaosblade https://chaosblade-io.github.io/chaosblade-helm 2>/dev/null || true
helm repo update
helm upgrade --install chaosblade chaosblade/chaosblade-operator \
  --namespace chaosblade --create-namespace --wait --timeout 5m

# 3. Online Boutique（受测系统）
kubectl create namespace online-boutique --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n online-boutique -f "$REPO/../example/microservices-demo/kubernetes-manifests"

# 4. 可观测栈（Jaeger + Prometheus，复用 observability 产物）
kubectl apply -f "$HERE/../observability/" 2>/dev/null || true

echo "等待 Pod 就绪..."
kubectl wait --for=condition=Ready pod -n online-boutique --all --timeout=300s
echo "环境就绪。kubeconfig: $(kind get kubeconfig --name cnaa > /tmp/cnaa-kind-kubeconfig && echo /tmp/cnaa-kind-kubeconfig)"
echo "评估: python cli.py assess --config <指向 kind 集群的 config>"