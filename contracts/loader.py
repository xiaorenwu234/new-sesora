"""Quality Contract Engine：加载/校验成熟度质量契约。

契约是评估的唯一判定标准来源。阈值一律引用预先冻结的 SLO 文件，
Agent 与实验结果均不得反向修改契约（方案 §4.1 的关键设计约束）。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from common.config import REPO_ROOT

REQUIRED_FIELDS = ["id", "name", "dimension", "applicability",
                   "static_evidence", "hypothesis", "verification", "oracle", "states"]

VALID_STATES = {"COMPLIANT", "VIOLATED", "RISK_UNCONFIRMED", "INCONCLUSIVE", "NOT_APPLICABLE"}


class Contract:
    def __init__(self, raw: dict):
        for f in REQUIRED_FIELDS:
            if f not in raw:
                raise ValueError(f"contract {raw.get('id')} missing field: {f}")
        if not set(raw["states"]) <= VALID_STATES:
            raise ValueError(f"contract {raw['id']} has invalid states")
        self.raw = raw
        self.id = raw["id"]
        self.name = raw["name"]
        self.dimension = raw["dimension"]
        self.severity = raw.get("severity", "medium")
        self.requires = raw["applicability"].get("requires", [])
        self.static_evidence = raw.get("static_evidence", [])
        self.static_check = raw.get("static_check")          # 确定性检查规则类型
        self.check_params = raw.get("check_params", {})
        self.hypothesis_statement = raw["hypothesis"]["statement"].strip()
        self.missing_evidence = raw["hypothesis"].get("missing_evidence", [])
        self.actions = raw["verification"].get("actions", [])
        self.workload = raw["verification"].get("workload", "predefined_business_scenario")
        self.oracle = raw["oracle"]
        self.dynamic_only = raw.get("dynamic_only", False)

    def oracle_thresholds(self, slo: dict, target: str | None = None) -> dict:
        """从冻结 SLO 文件解析阈值，绝不接受运行期传入的临时阈值。"""
        out = {}
        for metric, spec in self.oracle.get("metrics", {}).items():
            key = spec.get("slo_key") if isinstance(spec, dict) else spec
            if target:
                key = str(key).replace("{target}", target)
            node = slo
            for part in str(key).split("."):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            out[metric] = node
        return out

    def to_dict(self) -> dict:
        return self.raw


def load_contracts(contracts_dir: str | Path | None = None) -> list[Contract]:
    d = Path(contracts_dir) if contracts_dir else REPO_ROOT / "contracts"
    contracts = []
    for f in sorted(d.rglob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if doc:
                contracts.append(Contract(doc))
    ids = [c.id for c in contracts]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate contract ids")
    return contracts


def load_slo(path: str | Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)