# Helm 部署产物

本目录存放评估环境与受测系统的 Helm 部署配置。

## ChaosBlade Operator（故障注入引擎）

```bash
helm repo add chaosblade https://chaosblade-io.github.io/chaosblade-helm
helm upgrade --install chaosblade chaosblade/chaosblade-operator \
  --namespace chaosblade --create-namespace
```

`chaosblade-values.yaml` 为研究用途精简配置（限制并发实验、启用 CR 状态上报）。

## Online Boutique

源码仓库自带 `helm-chart/`，可直接：

```bash
helm upgrade --install boutique ../example/microservices-demo/helm-chart \
  --namespace online-boutique --create-namespace
```

## Astronomy Shop

见 `benchmarks/target_systems/astronomy_shop/deploy.sh`（open-telemetry/opentelemetry-demo chart）。

## Neo4j 与 kind

- Neo4j 证据图谱：`deployment/docker-compose/neo4j.yaml`
- 隔离测试集群：`deployment/kind/create-cluster.sh`