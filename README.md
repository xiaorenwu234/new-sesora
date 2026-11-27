# Cloud-Native Active Assessment (CNAA)

云原生架构成熟度主动评估系统。通过跨层证据采集、知识图谱建模、LLM Agent 自主假设生成与受控混沌实验闭环，对 Kubernetes 上运行的微服务系统进行自动化架构质量评估。

## 核心能力

- **跨层证据采集**：源码（Semgrep/Tree-sitter）、部署清单、K8s 运行时状态、Jaeger 链路追踪、Prometheus 指标、KubeLinter/Conftest 策略检查
- **证据图谱**：Neo4j 持久化 + NetworkX 内存镜像，实体对齐与关系推理
- **质量契约引擎**：六维度（弹性/可观测/一致性/可扩展/可部署/架构）声明式契约，SLO 阈值冻结
- **Agent 自主评估**：LangGraph 编排的假设-实验-反馈闭环，EGV 安全集内 Agent 终裁
- **受控混沌实验**：ChaosBlade 故障注入 + Locust 负载 + Oracle 自动判定 + 安全回收
- **只读数据深挖**：日志/链路/指标/配置/源码五类有界自主探索动作，不注入故障
- **自动 Onboard**：探测集群拓扑 + 源码扫描 → 自动生成画像/config/expose.sh → 验证采集
- **评估基线与消融**：静态/动态基线、多策略对比、组件消融实验

## 快速开始

### 环境要求

- Python 3.10+，conda 环境 `sesora`
- Kubernetes 集群（已部署目标系统 + Jaeger + Prometheus）
- Neo4j 5.x（可选，不可用时降级为纯内存模式）
- LLM 服务（OpenAI 兼容接口，可选；不可用时回退确定性启发式）
- ChaosBlade Operator（故障注入实验必需）
- Locust（负载生成）

### 安装

```bash
conda activate sesora
cd cloud-native-active-assessment
pip install -r requirements.txt
```

### 首次评估

```bash
# 1. 编辑 config.yaml，配置集群/源码/LLM/Neo4j 连接信息
# 2. 端到端评估（含动态实验）
python cli.py assess --max-experiments 3

# 3. 仅静态评估（不注入故障）
python cli.py assess --static-only

# 4. 使用 LangGraph 工作流编排
python cli.py assess --workflow --max-experiments 5
```

### 自动适配新系统

```bash
python cli.py onboard \
  --namespace my-app \
  --kubeconfig /path/to/kubeconfig \
  --repo-path /path/to/source \
  --config config.yaml
```

Onboard 流程自动完成：集群拓扑探测 → 源码扫描 → 服务分类 → NodePort 分配 → 生成画像/config/expose.sh → 执行暴露脚本 → 采集验证。产物位于 `benchmarks/target_systems/<system_id>/`。

## CLI 命令参考

| 命令 | 说明 |
|------|------|
| `collect` | 仅采集证据并建图 |
| `analyze` | 采集 + 确定性分析（不生成报告） |
| `assess` | 端到端评估（默认直接循环，`--workflow` 启用 LangGraph） |
| `experiment` | 手动执行单个受控实验（调试/复现） |
| `evaluate metrics` | 检测指标 P/R/F1 + 与基线对比 |
| `evaluate efficiency` | 单 run 效率指标 |
| `evaluate strategies` | 多策略效率对比 |
| `evaluate baseline` | 运行独立基线（static/dynamic） |
| `evaluate ablation` | 运行消融变体 |
| `evaluate run-strategy` | 运行单个策略对比实验 |
| `mutation list/apply/revert/revert-all/status` | 受控缺陷注入管理 |
| `cleanup` | 紧急清理所有 `cnaa-` 故障 CR |
| `onboard` | 自动适配新目标系统 |

### 常用参数

```bash
--config PATH        # 配置文件路径（默认 config.yaml）
--profile PATH       # 系统画像 YAML
--run-id ID          # 自定义运行 ID
--max-experiments N  # 覆盖预算中的最大实验数
--static-only        # 跳过动态实验
--workflow           # 使用 LangGraph 编排
```

### 手动实验示例

```bash
python cli.py experiment \
  --contract RES-001 \
  --target frontend \
  --action NETWORK_DELAY \
  --fault-target emailservice \
  --params '{"time_ms": 5000, "duration_s": 60}'
```

## 评估前准备

### 基础设施依赖

| 项目 | 必需 | 说明 |
|------|------|------|
| 目标系统已部署 | ✅ | 所有 Pod 处于 Running 状态 |
| kubeconfig | ✅ | 能访问目标命名空间 |
| Jaeger | ⚠️ 动态实验必需 | 查询 API 可达；不可用时仅可静态评估 |
| Prometheus | ⚠️ 动态实验必需 | RED 指标可查；不可用时降级为 Jaeger-only |
| ChaosBlade Operator | ⚠️ 故障注入必需 | CRD 已安装、cluster-scoped；`--static-only` 时不需要 |
| Neo4j | ❌ 可选 | 不可用时自动降级为纯内存 + JSON 落盘 |
| LLM 服务 | ❌ 可选 | OpenAI 兼容接口；不可用时回退确定性启发式 |
| NodePort 暴露 | ⚠️ 集群外访问时必需 | Jaeger/Prometheus/Frontend 从评估机可达 |

### 外部工具

确保以下工具在 PATH 中可用：

| 工具 | 用途 | 必需 |
|------|------|------|
| `kubectl` | 集群操作 | ✅ |
| `semgrep` | 源码静态分析 | ⚠️ 静态评估需要 |
| `kubelinter` | K8s 清单检查 | ⚠️ 静态评估需要 |
| `conftest` | Rego 策略检查 | ⚠️ 静态评估需要 |
| `locust` | 负载生成 | ⚠️ 动态实验需要 |

### 配置文件 (`config.yaml`)

按实际环境修改以下关键字段：

```yaml
system_id: online-boutique          # 与画像文件名对应

source:
  repo_path: /path/to/source        # 源码仓库绝对路径
  languages: [python, go, java]     # 按实际语言裁剪

kubernetes:
  kubeconfig: /path/to/kubeconfig   # 集群凭证
  namespace: online-boutique        # 目标命名空间
  manifest_dir: /path/to/manifests  # K8s 清单目录

runtime:
  jaeger_url: http://<node-ip>:<port>/jaeger/ui
  prometheus_url: http://<node-ip>:<port>

workload:
  frontend_url: http://<node-ip>:<port>
  locust_file: benchmarks/workloads/boutique_workload.py

slo_file: benchmarks/slo/boutique_slo.yaml

safety:
  allowed_namespaces: [online-boutique]
  protected_services: [redis-cart, loadgenerator]
```

完整配置段说明：

| 配置段 | 关键字段 | 说明 |
|--------|----------|------|
| `system_id` | - | 目标系统标识符 |
| `source` | `repo_path`, `commit`, `languages` | 源码仓库路径、Git commit、支持语言列表 |
| `kubernetes` | `kubeconfig`, `namespace`, `manifest_dir` | 集群访问凭证、命名空间、清单目录 |
| `runtime` | `jaeger_url`, `prometheus_url`, `lookback`, `trace_limit` | 遥测后端地址与查询窗口 |
| `neo4j` | `uri`, `user`, `password`, `database` | Neo4j 连接（不可用时自动降级） |
| `llm` | `base_url`, `api_key_env`, `model`, `temperature`, `enable_thinking` | OpenAI 兼容 LLM 配置 |
| `workload` | `frontend_url`, `locust_file`, `users`, `spawn_rate`, `duration_s` | Locust 负载配置 |
| `budget` | `max_experiments`, `max_wall_time_s`, `max_tokens`, `lambda_cost`, `mu_risk` | 评估预算与效用函数权重 |
| `agent` | `enabled`, `safe_set_k`, `max_new_hypotheses`, `max_proposals_per_round` | Agent 自主决策参数 |
| `exploration` | `max_steps`, `max_tokens_per_session`, `result_truncate_chars` | 只读深挖会话围栏 |
| `safety` | `allowed_namespaces`, `allowed_actions`, `max_fault_duration_s`, `protected_services` | 安全约束 |
| `slo_file` | - | 冻结 SLO 阈值文件路径 |
| `architecture` | `service_priorities`, `audit_log`, `docker_inspect` | 架构采集扩展源 |

使用 LLM 时需设置环境变量：`export CNAA_LLM_API_KEY=<key>`（本地 vLLM 设为 `local`）。

### 系统画像 (`benchmarks/systems/<system_id>.yaml`)

实体对齐的权威来源，定义评估系统如何理解目标系统：

```yaml
system_id: online-boutique
namespace: online-boutique

critical_services: [frontend, checkoutservice, ...]   # 关键业务服务
stateful_services: [redis-cart]                        # 有状态服务
infra_services: [loadgenerator]                        # 基础设施服务（不参与业务契约）

write_operations:                                      # 写操作标注（CONS 契约用）
  cartservice:
    - {op: cart_add, store: redis-cart, idempotent: unknown}

service_mapping:                                       # 核心：三名称对齐
  frontend:
    deployment: frontend       # K8s Deployment 名
    otel_name: frontend        # OTel service.name
    source_dir: src/frontend   # 源码子目录
```

新系统接入：运行 `python cli.py onboard` 自动生成，再人工校验 `service_mapping` 对齐准确性（`resolution_rate ≥ 0.9` 为合格线）。

### SLO 阈值文件 (`benchmarks/slo/<system>_slo.yaml`)

冻结的判定基准，评估开始后不可修改：

```yaml
global:
  error_rate_max: 0.01
  p99_latency_ms_max: 2000
  recovery_time_s_max: 60

services:
  frontend:
    error_rate_max: 0.01
    p99_latency_ms_max: 2000
```

### 负载脚本 (`benchmarks/workloads/<system>_workload.py`)

Locust 业务工作负载，定义固定场景以统一实验条件。需覆盖目标系统的关键业务路径（浏览、加购、下单等）。

### 质量契约 (`contracts/**/*.yaml`)

六维度声明式契约，每个契约包含：
- `id` / `name` / `dimension` / `severity`
- `applicability`：适用条件
- `static_evidence` / `static_check`：静态判定规则
- `hypothesis`：待验证风险陈述与缺失证据类型
- `verification.actions`：可用验证动作枚举
- `oracle`：判定逻辑与 SLO 阈值引用
- `states`：合法状态集合

### 评估前检查清单

逐项确认后再启动评估：

1. `kubectl --kubeconfig <kc> get pods -n <ns>` → 全部 Running
2. `curl <jaeger_url>/api/services` → 返回服务列表
3. `curl <prometheus_url>/api/v1/query?query=up` → 返回指标
4. `python cli.py collect` → 检查 `collection_meta.json` 中 `jaeger_available` / `prometheus_available` / `resolution_rate`
5. 动态实验：确认 ChaosBlade CRD 存在（`kubectl get crd | grep chaosblade`）
6. Agent 模式：确认 LLM 接口可达且 `CNAA_LLM_API_KEY` 已设置

> **最简启动路径**：仅跑 `--static-only` 时，只需 kubeconfig + 源码 + 画像 + SLO 四项，其余全部可缺省。

## 输出

每次评估在 `runs/<run-id>/` 下生成完整产物：

| 文件 | 内容 |
|------|------|
| `collection_meta.json` | 采集元数据：各层证据数量、对齐率、可用性状态 |
| `evidence_graph.json` | 完整证据图谱快照（节点 + 关系 + 对齐报告） |
| `experiments.jsonl` | 实验记录流：每行一个实验计划 + 判定结果 + 候选动作 |
| `report.json` | 结构化评估报告（机器可读） |
| `report.md` | Markdown 评估报告（人类可读） |
| `locust-*.csv` | Locust 负载统计（stats/failures/exceptions/history） |
| `explorations/*.jsonl` | 只读深挖会话记录（按实验 ID 分文件） |

### 报告结构

`report.json` 包含以下顶层字段：

- `system_metadata`：系统标识、运行 ID、LLM/图谱状态
- `coverage_summary`：适用契约-服务对数、已判定数、覆盖率
- `dimension_summary`：按维度聚合的违例/缺陷/合规/未确认统计
- `compliance_findings`：确定性规范违规（静态证据直接判定）
- `validated_runtime_findings`：动态实验证实的运行缺陷
- `unconfirmed_risks`：预算耗尽仍未验证的风险假设
- `cross_layer_evidence_chains`：违例发现的跨层证据链溯源
- `experiment_records`：全部实验执行记录
- `experiment_analyses`：Agent 对实验结果的分析与 followup 提议
- `selection_ranking_log`：EGV 候选排序日志
- `safety_decisions`：安全过滤器决策记录
- `budget`：预算使用情况
- `stop_reason`：停止原因（`budget_exhausted` / `all_resolved` / `no_safe_candidates` / `agent_declined_execution` / `static_only` / `iteration_cap`）

## 架构设计

### 评估管线

```
collect → deterministic → hypothesis_agent → [select → execute]* → finalize
```

1. **采集与建图**：七类采集器并行收集跨层证据，实体对齐后写入证据图谱
2. **确定性分析**：图谱查询 + 契约静态检查 → 直接判定合规/违规 + 生成待验证假设
3. **Agent 假设扩展**：HypothesisAgent 基于现有证据自主提出新假设
4. **候选选择**：ActionGenerator 枚举候选 + Proposer Agent 提议 → 安全过滤 → EGV 安全集 → Judge Agent 终裁
5. **受控执行**：基线负载 → 故障注入 → 生效确认 → 故障窗口观测 → 恢复验证 → Oracle 判定
6. **反馈迭代**：FeedbackUpdater 更新假设状态，共享实验关联兼容假设，Analyst followup 进入下一轮候选池
7. **报告生成**：去重 + 证据验证 + 维度聚合 + Markdown 渲染

### 数据模型

五种核心对象严格分离：

- **Evidence**：跨层事实单元，带来源、采集方法、位置、配置视图
- **Hypothesis**：待验证风险，证据不足时生成，绝不直接当作缺陷
- **Experiment**：受控实验计划与执行记录
- **Observation**：实验观测结果（聚合摘要 + 原始数据引用）
- **Finding**：最终评估结论，绑定契约、证据链与验证状态

### Agent 架构

- **HypothesisAgent**：基于图谱证据自主提出新假设
- **ExperimentProposer**：针对未解决假设提议候选实验
- **ExperimentJudge**：在 EGV 安全集内做最终选择决策
- **ResultAnalyst**：分析实验结果，生成 followup 提议形成闭环
- **ExperimentPlanner**：将候选动作规整为可执行实验计划

Agent 不可用或 `agent.enabled=false` 时，全链路回退纯 EGV top-1 选择（离线可复现）。

### 安全机制

- **命名空间白名单**：仅允许在指定命名空间执行故障注入
- **动作白名单**：仅允许预定义的验证动作
- **受保护服务**：基础设施/有状态服务不作为故障对象
- **故障时长上限**：单次故障最大持续时间
- **生效确认**：注入后双重验证（CR 状态 + 指标变化），无效实验标记 INEFFECTIVE
- **强制回收**：finally 块保证故障 CR 清理
- **中止检查**：故障窗口内检测到异常恶化立即中止
- **EGV 安全集**：Agent 仅在正效用 top-k 候选中做终裁，防止高风险选择

### 动作空间

| 类别 | 动作 | 说明 |
|------|------|------|
| 故障注入 | `NETWORK_DELAY`, `NETWORK_LOSS`, `DEPENDENCY_UNAVAILABLE` | 网络层故障 |
| 故障注入 | `POD_TERMINATION`, `CPU_PRESSURE`, `MEMORY_PRESSURE` | 资源/实例故障 |
| 故障注入 | `JVM_DELAY`, `JVM_EXCEPTION` | JVM 方法级故障 |
| 运维操作 | `LOAD_VARIATION`, `CONTROLLED_REDEPLOYMENT` | 负载变化/滚动更新 |
| 只读观测 | `OBSERVATION_ONLY` | 查询现有遥测，不注入故障 |
| 数据深挖 | `LOG_DEEP_DIVE`, `TRACE_DRILLDOWN`, `METRIC_DRILLDOWN`, `CONFIG_AUDIT`, `SOURCE_INSPECTION` | 有界多步自主探索 |

只读动作不占用实验配额、不注入故障、可查询受保护服务。

## 评估框架

### 检测指标

```bash
python cli.py evaluate metrics \
  --ours runs/full-e2e-06 \
  --baseline runs/static-baseline-test \
  --gt benchmarks/ground_truth/boutique_ground_truth.yaml
```

输出 Precision / Recall / F1 + 与基线的对比增量。

### 效率指标

```bash
# 单 run
python cli.py evaluate efficiency --ours runs/full-e2e-06 --budget 8

# 多策略对比
python cli.py evaluate strategies --runs egv=runs/a random=runs/b --budget 8
```

### 基线运行

```bash
# 静态基线
python cli.py evaluate baseline --type static

# 动态基线（随机选择）
python cli.py evaluate baseline --type dynamic --n 3 --seed 42
```

### 消融实验

```bash
python cli.py evaluate ablation --variant no_cost
```

### 受控缺陷注入

```bash
# 列出所有突变案例
python cli.py mutation list

# 注入缺陷到集群
python cli.py mutation apply MUT-001 --deploy

# 还原
python cli.py mutation revert MUT-001 --deploy
python cli.py mutation revert-all --deploy

# 查看已注入状态
python cli.py mutation status
```

## 目录结构

```
cloud-native-active-assessment/
├── cli.py                    # CLI 入口
├── config.yaml               # 全局配置
├── common/                   # 配置加载、LLM 客户端、数据模型
├── contracts/                # 质量契约定义（六维度 YAML）
├── collectors/               # 采集器
│   ├── source/               #   源码（Semgrep/Tree-sitter/Joern）
│   ├── deployment/           #   部署清单 + K8s API
│   └── runtime/              #   Jaeger/Prometheus/K8s Events
├── evidence_graph/           # 证据图谱（Neo4j + NetworkX）
├── analyzers/                # 确定性分析器 + Rego 策略
├── agents/                   # LLM Agent（假设/提议/裁判/分析/规划）
├── active_verification/      # EGV 选择器、动作生成、反馈更新、停止策略
├── experiment_runtime/       # 实验执行（ChaosBlade/Locust/Oracle/安全控制/深挖）
├── assessment/               # 管线编排、去重、验证、报告生成
├── onboarding/               # 自动适配新系统
├── evaluation/               # 评估框架（指标/基线/消融/策略对比）
├── benchmarks/               # 系统画像/SLO/Ground Truth/负载/突变案例
├── deployment/               # 部署辅助（Neo4j/ChaosBlade/Kind/Jaeger）
├── tests/                    # 单元测试
└── runs/                     # 评估产物输出目录
```

## 许可证

[MIT License](LICENSE)
