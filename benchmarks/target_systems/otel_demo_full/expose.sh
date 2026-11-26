#!/usr/bin/env bash
# otel-demo-full 评估入口暴露 + live manifest 导出（幂等）。
# 不改动 helm 管理的 Service，仅新增独立 NodePort Service；
# 并导出 deployment/service 活状态作为 DESIRED_CONFIG manifest 快照
# （helm 部署无独立 manifest 目录，manifest_collector 需要 yaml 文件）。
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-/home/xueht/new-sesora/kubeconfig}"
NS=otel-demo-full
HERE="$(cd "$(dirname "$0")" && pwd)"
MDIR="$HERE/manifests"
mkdir -p "$MDIR"

# 1. 独立 NodePort Service（jaeger query / prometheus / frontend-proxy）
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Service
metadata:
  name: jaeger-cnaa-query-full
  namespace: otel-demo-full
spec:
  type: NodePort
  selector:
    app.kubernetes.io/component: all-in-one
    app.kubernetes.io/instance: otel-demo-full
    app.kubernetes.io/name: jaeger
  ports:
  - {name: query, port: 16686, targetPort: 16686, nodePort: 31687}
---
apiVersion: v1
kind: Service
metadata:
  name: prometheus-cnaa-query
  namespace: otel-demo-full
spec:
  type: NodePort
  selector:
    app.kubernetes.io/component: server
    app.kubernetes.io/instance: otel-demo-full
    app.kubernetes.io/name: prometheus
  ports:
  - {name: http, port: 9090, targetPort: 9090, nodePort: 31091}
---
apiVersion: v1
kind: Service
metadata:
  name: frontend-proxy-cnaa
  namespace: otel-demo-full
spec:
  type: NodePort
  selector:
    opentelemetry.io/name: frontend-proxy
  ports:
  - {name: http, port: 8080, targetPort: 8080, nodePort: 31218}
EOF

# 2. 导出 live manifest（DESIRED_CONFIG 快照，供 manifest_collector 解析）
for d in $(kubectl get deploy -n "$NS" -o name); do
  n="${d#deployment.apps/}"
  kubectl get "$d" -n "$NS" -o yaml > "$MDIR/deployment-$n.yaml"
done
for s in $(kubectl get svc -n "$NS" -o name); do
  n="${s#service/}"
  kubectl get "$s" -n "$NS" -o yaml > "$MDIR/service-$n.yaml"
done

echo "exposed: jaeger=31687 prometheus=31091 frontend-proxy=31218 (node 192.168.5.51)"
echo "manifests exported: $MDIR ($(ls "$MDIR" | wc -l) files)"
