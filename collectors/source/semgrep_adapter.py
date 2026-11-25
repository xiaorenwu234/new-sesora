"""Semgrep 适配器：用自定义成熟度模式规则扫描源码，输出带位置的 Evidence。

复用 Semgrep 的模式匹配能力；规则独立定义（不借用 SESORA 检查器）。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from common.config import REPO_ROOT, Config, git_commit
from common.models import Evidence

RULES = REPO_ROOT / "collectors" / "source" / "rules" / "maturity_patterns.yaml"
EXCLUDES = ["genproto", "node_modules", "tests", "test", ".git", "build", "dist",
            "gradle", "obj", "bin", "pb", "generated"]


def service_of_path(path: str, profile: dict) -> str | None:
    """按系统画像的显式 source_dir 映射归属服务；无法归属返回 None。"""
    for svc, m in (profile.get("service_mapping") or {}).items():
        sd = m.get("source_dir")
        if sd and (path.startswith(sd + "/") or path == sd):
            return svc
    return None


def collect(cfg: Config, profile: dict) -> list[Evidence]:
    repo = Path(cfg.source.repo_path)
    src = repo / profile.get("source_root", "src")
    commit = cfg.source.get("commit") or git_commit(str(repo))
    env = dict(os.environ, SEMGREP_SEND_METRICS="off", SEMGREP_ENABLE_VERSION_CHECK="0")
    cmd = ["semgrep", "scan", "--config", str(RULES), "--json", "--quiet",
           "--metrics=off", "--disable-version-check", "--timeout", "60"]
    for ex in EXCLUDES:
        cmd += ["--exclude", ex]
    cmd.append(str(src))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=env, timeout=900, cwd=str(repo))
        data = json.loads(proc.stdout or "{}")
    except FileNotFoundError:
        raise RuntimeError("semgrep 未安装：pip install semgrep（sesora 环境）")
    except subprocess.TimeoutExpired:
        raise RuntimeError("semgrep 扫描超时")

    evidence: list[Evidence] = []
    for r in data.get("results", []):
        meta = r.get("extra", {}).get("metadata", {}) or {}
        kind = meta.get("cnaa_kind")
        if not kind:
            continue
        rel = str(Path(r["path"]).relative_to(repo)) if Path(r["path"]).is_absolute() else r["path"]
        svc = service_of_path(rel, profile)
        if svc is None:
            continue  # 无法归属服务的命中不进入图谱（保留未解析原则）
        evidence.append(Evidence(
            kind=kind, layer="source", subject=svc,
            attrs={"value": meta.get("cnaa_value", "present"),
                   "rule_id": r.get("check_id", ""),
                   "snippet": (r.get("extra", {}).get("lines") or "").strip()[:300]},
            source="semgrep", method=f"semgrep rule {r.get('check_id')}",
            location=f"{rel}:{r['start']['line']}",
            system_id=cfg.system_id, version=commit,
        ))
    return evidence