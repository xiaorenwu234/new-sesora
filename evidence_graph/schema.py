"""Evidence Graph Schema（方案 §4.3 第一版实体与关系）。"""

NODE_LABELS = [
    "Service", "Function", "Deployment", "Config", "TraceRef", "MetricWindow",
    "Contract", "Hypothesis", "Experiment", "Observation", "Finding", "Evidence",
]

REL_TYPES = [
    "IMPLEMENTS",      # Service -> Function
    "DEPLOYED_AS",     # Service -> Deployment
    "DEPENDS_ON",      # Service -> Service（区分 static_hint / runtime）
    "CALLS",           # Function -> Function（服务内）
    "USES_CONFIG",     # Function/Service -> Config
    "OBSERVES",        # TraceRef/MetricWindow -> Service
    "ABOUT",           # Evidence -> 实体
    "APPLIES_TO",      # Contract -> Service
    "CONCERNS",        # Hypothesis -> Contract
    "TARGETS",         # Hypothesis/Experiment -> Service
    "SUPPORTED_BY",    # Hypothesis -> Evidence
    "REFUTED_BY",      # Hypothesis -> Evidence
    "TESTS",           # Experiment -> Hypothesis
    "PRODUCES",        # Experiment -> Observation
    "VALIDATED_BY",    # Finding -> Observation
    "DERIVED_FROM",    # Finding -> Hypothesis/Evidence
]

CONSTRAINTS = [
    "CREATE CONSTRAINT svc_key IF NOT EXISTS FOR (n:Service) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT ev_id IF NOT EXISTS FOR (n:Evidence) REQUIRE n.evidence_id IS UNIQUE",
    "CREATE CONSTRAINT hyp_id IF NOT EXISTS FOR (n:Hypothesis) REQUIRE n.hypothesis_id IS UNIQUE",
    "CREATE CONSTRAINT exp_id IF NOT EXISTS FOR (n:Experiment) REQUIRE n.experiment_id IS UNIQUE",
    "CREATE CONSTRAINT obs_id IF NOT EXISTS FOR (n:Observation) REQUIRE n.observation_id IS UNIQUE",
    "CREATE CONSTRAINT find_id IF NOT EXISTS FOR (n:Finding) REQUIRE n.finding_id IS UNIQUE",
    "CREATE CONSTRAINT contract_id IF NOT EXISTS FOR (n:Contract) REQUIRE n.contract_id IS UNIQUE",
]