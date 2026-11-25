"""受控缺陷注入 runner（方案 §8）。

declarative mutation：对 manifest YAML 按路径 set/delete，git checkout 还原。
manifest 层案例可自动 apply/revert，并可选 kubectl apply 部署到集群使缺陷
生效（无需重建镜像）；source 层案例需重建镜像，runner 仅修改文件。

用法：
  apply_mutation(cfg, "MUT-001", deploy=True)   # 注入并部署
  revert_mutation(cfg, "MUT-001", deploy=True)  # 还原并重新部署原始配置
  revert_all(cfg)                               # 还原所有已注入案例
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from common.config import Config

_HERE = Path(__file__).resolve().parent
_STATE_FILE = "mutations_state.json"


def load_cases(path: str | Path | None = None) -> list[dict]:
    return yaml.safe_load(Path(path or _HERE / "cases.yaml").read_text()) or []


def get_case(case_id: str, path: str | Path | None = None) -> dict:
    for c in load_cases(path):
        if c["id"] == case_id:
            return c
    raise KeyError(f"unknown mutation case {case_id}")


def _split_path(path: str) -> list:
    """spec.template.spec.containers[0].readinessProbe -> ['spec',...,('containers',0),...]"""
    parts = []
    for seg in path.split("."):
        m = re.match(r"([^\[]+)\[(\d+)\]", seg)
        if m:
            parts.append(m.group(1))
            parts.append(int(m.group(2)))
        else:
            parts.append(seg)
    return parts


def _navigate(doc: dict, parts: list):
    node = doc
    for p in parts[:-1]:
        node = node[p]
    return node, parts[-1]


def _apply_op(doc: dict, mutation: dict):
    parts = _split_path(mutation["path"])
    parent, last = _navigate(doc, parts)
    if mutation["op"] == "set":
        parent[last] = mutation["value"]
    elif mutation["op"] == "delete":
        if isinstance(parent, list):
            del parent[last]
        else:
            parent.pop(last, None)
    else:
        raise ValueError(f"unknown op {mutation['op']}")


def _git(repo: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True, timeout=30)


def _kubectl_apply(cfg: Config, file: Path):
    return subprocess.run(
        ["kubectl", "--kubeconfig", cfg.kubernetes.kubeconfig, "apply", "-f", str(file)],
        capture_output=True, text=True, timeout=60)


def _state_path(cfg: Config) -> Path:
    return cfg.resolve(cfg.get("output_dir", "runs")) / _STATE_FILE


def _load_state(cfg: Config) -> dict:
    p = _state_path(cfg)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(cfg: Config, state: dict):
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def apply_mutation(cfg: Config, case_id: str, deploy: bool = False,
                   cases_path: str | Path | None = None) -> dict:
    case = get_case(case_id, cases_path)
    repo = cfg.source.repo_path
    file = Path(repo) / case["file"]
    docs = list(yaml.safe_load_all(file.read_text()))
    # 仅修改第一个匹配 kind=Deployment 的文档
    target_doc = next((d for d in docs if isinstance(d, dict)
                       and d.get("kind") == "Deployment"), docs[0])
    _apply_op(target_doc, case["mutation"])
    file.write_text(yaml.safe_dump_all(docs, sort_keys=False))

    state = _load_state(cfg)
    state[case_id] = {"file": case["file"], "deployed": deploy}
    _save_state(cfg, state)

    result = {"case_id": case_id, "file": case["file"], "applied": True}
    if deploy and case["layer"] == "manifest":
        p = _kubectl_apply(cfg, file)
        result["deploy"] = {"returncode": p.returncode, "stdout": p.stdout.strip()}
    return result


def revert_mutation(cfg: Config, case_id: str, deploy: bool = False,
                    cases_path: str | Path | None = None) -> dict:
    case = get_case(case_id, cases_path)
    repo = cfg.source.repo_path
    p = _git(repo, "checkout", "--", case["file"])
    state = _load_state(cfg)
    was_deployed = state.pop(case_id, {}).get("deployed", False)
    _save_state(cfg, state)
    result = {"case_id": case_id, "reverted": p.returncode == 0,
              "git_stderr": p.stderr.strip()}
    if deploy and was_deployed and case["layer"] == "manifest":
        kp = _kubectl_apply(cfg, Path(repo) / case["file"])
        result["deploy"] = {"returncode": kp.returncode, "stdout": kp.stdout.strip()}
    return result


def revert_all(cfg: Config, deploy: bool = False) -> list[dict]:
    state = _load_state(cfg)
    return [revert_mutation(cfg, cid, deploy=deploy) for cid in list(state)]


def applied_cases(cfg: Config) -> list[str]:
    return list(_load_state(cfg))