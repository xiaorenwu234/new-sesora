"""配置加载与运行上下文。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class Config(dict):
    """dict 子类，支持 cfg.source.repo_path 式访问。"""

    def __getattr__(self, name):
        try:
            v = self[name]
        except KeyError:
            raise AttributeError(name)
        return Config(v) if isinstance(v, dict) else v

    @staticmethod
    def load(path: str | Path | None = None) -> "Config":
        path = Path(path) if path else REPO_ROOT / "config.yaml"
        with open(path) as f:
            cfg = Config(yaml.safe_load(f))
        cfg["_config_path"] = str(path)
        return cfg

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else REPO_ROOT / p


def git_commit(repo_path: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def new_run_dir(cfg: Config, run_id: str) -> Path:
    d = cfg.resolve(cfg.get("output_dir", "runs")) / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d