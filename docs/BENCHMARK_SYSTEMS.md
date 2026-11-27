# Benchmark 系统指南

CNAA 内置四套受测系统画像，覆盖不同技术栈、规模和可观测性成熟度。每套系统包含：系统画像、评估配置、SLO 阈值、负载脚本，以及可选的部署/暴露脚本。

## 系统总览

| 系统 | system_id | 命名空间 | 服务数 | 技术栈 | 可观测栈 | 适用场景 |
|------|-----------|----------|--------|--------|----------|----------|
| Online Boutique | `online-boutique` | `online-boutique` | 12 | Go/Node.js/Python/C#/Java | Jaeger（外部） | 默认评估目标，轻量级电商 |
| OTel Demo Full | `otel-demo-full` | `otel-demo-full` | 28 | Go/Python/Java/JS/C#/Ruby/Rust/C++ | Jaeger + Prometheus + Grafana + OpenSearch | 完整可观测栈，多语言微服务 |
| Astronomy Shop | `astronomy-shop` | `otel-demo` | 15 | 同 OTel Demo（裁剪版） | Jaeger（外部） | RQ5 跨系统对比评估 |
| TrainTicket | `train_ticket` | `train-ticket` | 46+ | Java Spring Cloud | 无（纯静态评估） | 大规模遗留系统适配验证 |

---

## Online Boutique

Google Cloud 经典微服务演示应用，模拟在线零售商店。

### 架构

- **前端**：Next.js（Node.js），聚合所有后端服务
- **业务服务**：Go（productcatalog/currency/shipping/recommendation）、C#（cart + redis-cart）、Python（email/ad）、Java（payment/checkout）
- **有状态**：redis-cart（购物车存储）
- **基础设施**：loadgenerator（Locust 自动负载）

### 源码位置

```
example/microservices-demo/
├── src/                    # 各服务源码
├── kubernetes-manifests/   # K8s 部署清单
└── helm-chart/             # Helm chart
```

### 部署

Online Boutique 使用原生 K8s manifests 部署：

```bash
kubectl apply -f example/microservices-demo/kubernetes-manifests/ --namespace online-boutique
```

或使用 Helm：

```bash
helm install online-boutique example/microservices-demo/helm-chart/ \
  -n online-boutique --create-namespace
```

### 可观测性

Online Boutique 自身不带 Jaeger/Prometheus，traces 通过 `COLLECTOR_SERVICE_ADDR` 环境变量发送到外部 OTel Collector（如 `otel-demo` 命名空间的 Jaeger）。需确保：

- Jaeger Query API 从评估机可达（NodePort 或 port-forward）
- 各服务的 `OTEL_SERVICE_NAME` 与画像中 `service_mapping.otel_name` 一致

### 评估命令

```bash
# 默认配置即为 Online Boutique
python cli.py assess --max-experiments 3

# 仅静态评估
python cli.py assess --static-only
```

### 关键文件

| 文件 | 说明 |
|------|------|
| `benchmarks/systems/online_boutique.yaml` | 系统画像 |
| `config.yaml` | 评估配置（默认） |
| `benchmarks/slo/boutique_slo.yaml` | SLO 阈值 |
| `benchmarks/workloads/boutique_workload.py` | Locust 负载 |
| `benchmarks/ground_truth/boutique_ground_truth.yaml` | Ground Truth（检测指标计算用） |

---

## OTel Demo Full（OpenTelemetry Astronomy Shop 完整版）

OpenTelemetry 官方演示应用的完整部署形态，包含全部微服务 + 完整可观测栈（Jaeger + Prometheus + Grafana + OpenSearch）。

### 架构

- **业务服务（14 个）**：frontend / frontend-proxy / checkout / cart / payment / product-catalog / currency / shipping / email / ad / recommendation / quote / accounting / fraud-detection / chatbot
- **有状态服务**：astronomy-db（PostgreSQL）、valkey-cart（Redis 兼容）、kafka（消息队列）、opensearch（日志存储）
- **可观测基础设施**：otel-collector（DaemonSet Agent + Gateway）、jaeger、prometheus、grafana、flagd（Feature Flag）、image-provider、mcp、opamp-server、telemetry-docs
- **负载生成**：load-generator（内置 Locust）

### 源码位置

```
example/opentelemetry-demo/
├── src/                    # 各服务源码（多语言）
└── ...
```

源码 tag 与 Helm chart appVersion 对齐（3.0.0，无 v 前缀）。

### 部署

使用本地 vendor 的 Helm chart 部署（绕过外网依赖下载限制）：

```bash
# 1. 运行暴露脚本（创建 NodePort + 导出 live manifests，幂等）
bash benchmarks/target_systems/otel_demo_full/expose.sh

# 2. Helm 安装（chart 位于 .otel-work/build/opentelemetry-demo）
helm install otel-demo-full .otel-work/build/opentelemetry-demo \
  -f .otel-work/full-values.yaml \
  -n otel-demo-full --create-namespace
```

部署后约 30 个 Pod Running。首次启动时 accounting/checkout/fraud-detection 会等待 kafka broker 就绪，属正常行为。

### 访问入口

| 服务 | NodePort | URL |
|------|----------|-----|
| Frontend Proxy | 31218 | `http://<node-ip>:31218/` |
| Jaeger UI | 31687 | `http://<node-ip>:31687/jaeger/ui/` |
| Prometheus | 31091 | `http://<node-ip>:31091/` |
| Grafana | — | 通过 frontend-proxy `/grafana/` 路由 |

> Jaeger 2.x API 必须带 base path `/jaeger/ui`，裸 `/api/services` 返回 404。

### 评估命令

```bash
python cli.py assess \
  --config benchmarks/target_systems/otel_demo_full/config.yaml \
  --profile benchmarks/systems/otel_demo_full.yaml \
  --max-experiments 5
```

### 已知限制

- 集群资源紧张时部分组件可能 Pending，需调降 requests 或暂停其他命名空间负载
- Helm chart 带 `values.schema.json` 严格校验，自定义字段需通过 kubectl patch 而非 values 传入
- 同集群二次安装需改名 ClusterRole/ClusterRoleBinding 避免冲突

### 关键文件

| 文件 | 说明 |
|------|------|
| `benchmarks/systems/otel_demo_full.yaml` | 系统画像（28 Deployment） |
| `benchmarks/target_systems/otel_demo_full/config.yaml` | 评估配置 overlay |
| `benchmarks/target_systems/otel_demo_full/expose.sh` | NodePort 暴露 + manifest 导出 |
| `benchmarks/target_systems/otel_demo_full/manifests/` | live deployment/service 快照 |
| `benchmarks/slo/astronomy_slo.yaml` | SLO 阈值（与 Astronomy Shop 共用） |
| `benchmarks/workloads/astronomy_workload.py` | Locust 负载 |

---

## Astronomy Shop（OTel Demo 裁剪版）

与 OTel Demo Full 同一 Helm chart（opentelemetry-demo-0.41.0 / appVersion 3.0.0），但仅启用核心业务组件，用于 RQ5 跨系统对比评估。

### 与 OTel Demo Full 的区别

| 维度 | Astronomy Shop | OTel Demo Full |
|------|---------------|----------------|
| 命名空间 | `otel-demo` | `otel-demo-full` |
| 组件数 | ~15（核心业务） | ~30（全量） |
| 可观测栈 | 仅 Jaeger（复用外部） | Jaeger + Prometheus + Grafana + OpenSearch |
| Kafka/Accounting/FraudDetection | 关闭 | 启用 |
| 用途 | 轻量对比基线 | 完整评估 |

### 部署

```bash
# 使用 deploy.sh 拉起裁剪版
bash benchmarks/target_systems/astronomy_shop/deploy.sh
```

### 评估命令

```bash
python cli.py assess \
  --config benchmarks/target_systems/astronomy_shop/config.yaml \
  --profile benchmarks/systems/astronomy_shop.yaml \
  --max-experiments 5
```

### 关键文件

| 文件 | 说明 |
|------|------|
| `benchmarks/systems/astronomy_shop.yaml` | 系统画像 |
| `benchmarks/target_systems/astronomy_shop/config.yaml` | 评估配置 overlay |
| `benchmarks/target_systems/astronomy_shop/deploy.sh` | 部署脚本 |

---

## TrainTicket

复旦大学 FudanSELab 开发的火车票预订系统，46+ 个 Java Spring Cloud 微服务，用于验证评估框架对大规模遗留系统的适配能力。

### 架构

- **网关**：ts-gateway-service（Spring Cloud Gateway）
- **核心业务**：ts-order-service / ts-preserve-service / ts-payment-service / ts-travel-service / ts-seat-service / ts-ticket-office-service 等
- **管理后台**：ts-admin-order-service / ts-admin-user-service / ts-admin-route-service / ts-admin-travel-service / ts-admin-basic-info-service
- **辅助服务**：ts-auth-service / ts-verification-code-service / ts-notification-service / ts-news-service / ts-avatar-service 等
- **有状态**：nacosdb-mysql（Nacos 配置中心数据库）、tsdb-mysql（业务数据库）、rabbitmq（消息队列）、nacos（服务注册/配置中心）
- **前端**：ts-ui-dashboard

### 源码位置

```
example/train-ticket/
├── ts-*/                   # 各服务源码（Java Maven 项目）
└── deployment/kubernetes-manifests/quickstart-k8s/
    ├── yamls/deploy.yaml   # 主部署文件（从 deploy.yaml.sample 复制）
    └── charts/             # MySQL/Nacos/RabbitMQ Helm charts
```

### 部署

TrainTicket 部署较为复杂，需按顺序执行：

```bash
cd example/train-ticket/deployment/kubernetes-manifests/quickstart-k8s

# 1. 基础设施
helm install nacosdb-mysql charts/mysql/ -n train-ticket --create-namespace
helm install nacos charts/nacos/ -n train-ticket
helm install rabbitmq charts/rabbitmq/ -n train-ticket

# 2. 业务数据库
helm install tsdb-mysql charts/mysql/ -n train-ticket \
  --set mysql.database=tsdb

# 3. 生成 MySQL Secrets（每个有独立库的服务需要）
# 参考 hack/deploy/gen-mysql-secret.sh

# 4. 部署业务服务
kubectl apply -f yamls/svc.yaml -n train-ticket
kubectl apply -f yamls/deploy.yaml -n train-ticket
```

> **注意**：仓库只提供 `deploy.yaml.sample`，需手动复制为 `deploy.yaml`；镜像前缀需改为可达代理（如 `docker.m.daocloud.io/codewisdom/`）。

### 可观测性

TrainTicket 默认不带 OTel 可观测栈。当前评估以纯 K8s API 静态证据为主（`jaeger_available=false`, `prometheus_available=false`）。如需动态实验，需自行接入 OTel SDK 并部署 Jaeger/Prometheus。

### Onboard 自动适配

TrainTicket 的画像和配置由 onboard 自动生成：

```bash
python cli.py onboard \
  --namespace train-ticket \
  --kubeconfig /path/to/kubeconfig \
  --repo-path example/train-ticket \
  --config config.yaml
```

生成产物：
- `benchmarks/systems/train_ticket.yaml`（resolution_rate=0.988）
- `benchmarks/target_systems/train_ticket/config.yaml`
- `benchmarks/target_systems/train_ticket/expose.sh`

### 评估命令

```bash
# 静态分析（推荐，semgrep 全量扫描 Java Maven 仓库耗时较长）
python cli.py analyze \
  --config benchmarks/target_systems/train_ticket/config.yaml \
  --profile benchmarks/systems/train_ticket.yaml

# 仅采集建图
python cli.py collect \
  --config benchmarks/target_systems/train_ticket/config.yaml \
  --profile benchmarks/systems/train_ticket.yaml
```

> **注意**：`assess --static-only` 对大型 Java Maven 仓库可能因 semgrep 全量扫描超时，建议改用 `analyze` 子命令。

### 已知限制

- semgrep 规则与 Java Maven 项目结构不完全匹配，`source_evidence` 可能为 0
- SLO 文件暂复用 `astronomy_slo.yaml`，需根据 TrainTicket 实际 SLA 调整
- 负载脚本暂复用 `astronomy_workload.py`，需按 TrainTicket API 重写
- 集群 CPU 资源紧张时部分服务可能 CrashLoop（依赖 tsdb-mysql 可用性）

### 关键文件

| 文件 | 说明 |
|------|------|
| `benchmarks/systems/train_ticket.yaml` | 系统画像（onboard 生成） |
| `benchmarks/target_systems/train_ticket/config.yaml` | 评估配置（onboard 生成） |
| `benchmarks/target_systems/train_ticket/expose.sh` | NodePort 暴露脚本 |

---

## 接入新系统

对于不在上述列表中的系统，使用 onboard 自动适配：

```bash
python cli.py onboard \
  --namespace <your-namespace> \
  --kubeconfig /path/to/kubeconfig \
  --repo-path /path/to/source \
  --config config.yaml
```

Onboard 自动完成：

1. 探测集群拓扑（Deployment/Service/Pod）
2. 扫描源码仓库（语言检测、服务目录识别）
3. 服务分类（关键/有状态/基础设施）
4. 分配 NodePort 端口
5. 生成系统画像、评估配置、暴露脚本
6. 执行暴露脚本
7. 运行采集验证（检查 resolution_rate ≥ 0.9）

生成产物位于 `benchmarks/target_systems/<system_id>/`，人工校验后即可用于评估。
