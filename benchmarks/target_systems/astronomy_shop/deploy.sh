#!/usr/bin/env bash
# 部署完整 Astronomy Shop（OpenTelemetry Demo）到 otel-demo 命名空间。
# 需要：helm、kubectl（KUBECONFIG）、国外网络（镜像 ghcr.io）。
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-/home/xueht/new-sesora/kubeconfig}"
NS=otel-demo
SRC="${HOME}/opentelemetry-demo"

# 1. 源码（用于源码层采集与 mutation）
if [ ! -d "$SRC" ]; then
  git clone --depth 1 https://github.com/open-telemetry/opentelemetry-demo "$SRC"
fi

# 2. Helm 部署（启用 jaeger/prometheus 可观测栈）
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts 2>/dev/null || true
helm repo update
helm upgrade --install otel-demo open-telemetry/opentelemetry-demo \
  --namespace "$NS" --create-namespace \
  --set observability.jaeger.enabled=true \
  --set observability.prometheus.enabled=true \
  --set components.load-generator.enabled=true \
  --wait --timeout 15m

# 3. 暴露 frontend NodePort 31217（与 config.yaml workload.frontend_url 一致）
kubectl -n "$NS" patch svc frontend -p \
  '{"spec":{"type":"NodePort","ports":[{"port":8080,"targetPort":8080,"nodePort":31217,"name":"http"}]}}' \
  2>/dev/null || \
kubectl -n "$NS" expose deploy frontend --type=NodePort --port=8080 --target-port=8080 \
  --overrides='{"spec":{"ports":[{"port":8080,"targetPort":8080,"nodePort":31217}]}}'

echo "Astronomy Shop deployed. frontend: http://<node-ip>:31217"
echo "评估: python cli.py assess --config benchmarks/target_systems/astronomy_shop/config.yaml --profile benchmarks/systems/astronomy_shop.yaml"