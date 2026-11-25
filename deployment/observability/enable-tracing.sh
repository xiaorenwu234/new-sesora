#!/usr/bin/env bash
# 为 Online Boutique 启用 OTel tracing（幂等）。
# 7 个服务源码支持 ENABLE_TRACING=1 + COLLECTOR_SERVICE_ADDR（OTLP gRPC）。
# shippingservice/cartservice/adservice 当前版本无可用插桩，跳过。
set -euo pipefail
NS="${NAMESPACE:-online-boutique}"
COLLECTOR="${COLLECTOR_ADDR:-jaeger.otel-demo.svc.cluster.local:4317}"
SERVICES="frontend checkoutservice productcatalogservice currencyservice paymentservice emailservice recommendationservice"

for svc in $SERVICES; do
  existing=$(kubectl get deploy "$svc" -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ENABLE_TRACING")].value}')
  if [ "$existing" = "1" ]; then
    echo "$svc: tracing already enabled, skip"
  else
    kubectl patch deploy "$svc" -n "$NS" --type=json -p "[
      {\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/env/-\",
       \"value\":{\"name\":\"ENABLE_TRACING\",\"value\":\"1\"}},
      {\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/env/-\",
       \"value\":{\"name\":\"COLLECTOR_SERVICE_ADDR\",\"value\":\"$COLLECTOR\"}}]"
    echo "$svc: tracing enabled -> $COLLECTOR"
  fi
  # Go/Python SDK 默认 resource 读 OTEL_SERVICE_NAME，否则上报 unknown_service
  case "$svc" in frontend|checkoutservice|productcatalogservice|emailservice|recommendationservice)
    name=$(kubectl get deploy "$svc" -n "$NS" \
      -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="OTEL_SERVICE_NAME")].value}')
    if [ "$name" != "$svc" ]; then
      kubectl patch deploy "$svc" -n "$NS" --type=json -p "[
        {\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/env/-\",
         \"value\":{\"name\":\"OTEL_SERVICE_NAME\",\"value\":\"$svc\"}}]"
      echo "$svc: OTEL_SERVICE_NAME set"
    fi ;;
  esac
done

for svc in $SERVICES; do
  kubectl rollout status deploy "$svc" -n "$NS" --timeout=300s
done
echo "done."