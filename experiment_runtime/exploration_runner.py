"""有界多步数据深挖执行器（只读，不注入故障）。

Agent 在工具白名单内自主决定"查什么、往哪挖"：每步 LLM 结构化输出
ExplorationStep{done, tool, args, reason}，工具原始输出经确定性解析器
计算指标（不接受 LLM 自报数值），发现构造 Evidence 入图谱。

围栏：步数上限 exploration.max_steps、会话 token 上限
exploration.max_tokens_per_session、连续 2 次非法调用提前终止。
LLM 不可用 -> 按动作类型执行一次默认查询（确定性回退，离线可复现）。
审计：run_dir/explorations/<experiment_id>.jsonl 每步完整记录。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import BaseModel, Field

from experiment_runtime.exploration_tools import ExplorationTools, TOOL_SPECS

SYSTEM = (
    "你是云原生评估系统的只读数据深挖探索器。目标：为给定契约假设补充缺失证据。"
    "每步从工具白名单中选择一个工具并给出参数，基于已有发现决定下一步往哪挖"
    "（如日志发现异常 -> 钻取对应 trace -> 核对源码）。"
    "证据已足够或无进一步线索时置 done=true。"
    "禁止编造数值与事实，所有结论必须来自工具返回；禁止请求白名单外的工具。")


class ExplorationStep(BaseModel):
    done: bool = Field(default=False, description="证据已足够或无进一步线索")
    tool: str = Field(default="", description="工具名（白名单内）")
    args: dict = Field(default_factory=dict, description="工具参数")
    reason: str = Field(default="", description="本步深挖理由")


# 各深挖动作的确定性默认查询（LLM 不可用时的回退）
def _default_calls(action: str, target: str, params: dict) -> list[tuple[str, dict]]:
    lb = int(params.get("lookback_s", 600))
    if action == "LOG_DEEP_DIVE":
        return [("pod_logs", {"service": target, "since_s": lb,
                              "tail_lines": int(params.get("tail_lines", 500)),
                              "keyword": params.get("keyword", "")})]
    if action == "TRACE_DRILLDOWN":
        return [("trace_search", {"service": target, "lookback_s": lb,
                                  "limit": int(params.get("limit", 20)),
                                  "error_only": bool(params.get("error_only"))})]
    if action == "CONFIG_AUDIT":
        return [("k8s_inspect", {"kind": "events"}),
                ("k8s_inspect", {"kind": "deployment", "name": target})]
    if action == "SOURCE_INSPECTION":
        return [("source_inspect", {"service": target})]
    return []   # METRIC_DRILLDOWN 回退走 service_red（见 run_exploration）


def _aggregate(agg: dict, m: dict):
    """确定性指标聚合：计数累加、比率取最近一次实测、存在性取 OR。"""
    for k in ("error_log_count", "error_span_count", "log_line_count",
              "warning_event_count", "trace_count"):
        if k in m:
            agg[k] = agg.get(k, 0) + m[k]
    if "broken_trace_ratio" in m and m.get("trace_count", 0) > 0:
        agg["broken_trace_ratio"] = m["broken_trace_ratio"]
    if "metric_series_present" in m:
        agg["metric_series_present"] = agg.get("metric_series_present", False) \
            or m["metric_series_present"]
    if "restart_count" in m:
        agg["restart_count"] = max(agg.get("restart_count", 0), m["restart_count"])


def run_exploration(cfg, graph, experiment, contract, llm,
                    run_dir: Path | None = None, tools=None) -> dict:
    """执行有界多步深挖，返回聚合指标（Observation 由 controller 组装）。"""
    ecfg = cfg.get("exploration", {}) or {}
    max_steps = int(experiment.params.get("max_steps",
                                          ecfg.get("max_steps", 6)))
    max_tokens = int(ecfg.get("max_tokens_per_session", 20000))
    trunc = int(ecfg.get("result_truncate_chars", 2000))
    tools = tools or ExplorationTools(cfg, graph)
    target = experiment.observe_target or experiment.target
    action = experiment.action_type

    audit_path = None
    if run_dir:
        audit_path = Path(run_dir) / "explorations" / f"{experiment.experiment_id}.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)

    def audit(rec: dict):
        if audit_path:
            with open(audit_path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    agg: dict = {}
    findings: list[dict] = []
    steps, invalid_streak, terminated = 0, 0, "max_steps"
    tokens_start = getattr(llm, "tokens_used", 0)

    llm_ok = llm is not None and getattr(llm, "available", lambda: False)()
    if not llm_ok:
        # 确定性回退：单发默认查询
        calls = _default_calls(action, target, experiment.params)
        if action == "METRIC_DRILLDOWN":
            from collectors.runtime import prometheus_collector as prom
            if prom.available(cfg):
                red = prom.service_red(cfg, target,
                                       window=f"{int(experiment.params.get('lookback_s', 600))}s")
                agg["metric_series_present"] = bool(
                    red.get("available") and red.get("request_count", 0) > 0)
        for tool, args in calls:
            res = tools.call(tool, args)
            steps += 1
            if res.get("ok"):
                _aggregate(agg, res.get("metrics", {}))
            audit({"step": steps, "tool": tool, "args": args,
                   "ok": res.get("ok"), "metrics": res.get("metrics", {}),
                   "error": res.get("error"), "mode": "deterministic_fallback"})
        terminated = "deterministic_fallback"
    else:
        for i in range(max_steps):
            prompt = (
                f"深挖动作: {action}\n目标服务: {target}\n"
                f"契约: {contract.id} {contract.name}\n"
                f"待补证据: {experiment.expected_evidence}\n"
                f"工具白名单:\n" + "\n".join(
                    f"- {k}: {v}" for k, v in TOOL_SPECS.items()) + "\n"
                f"已有发现({len(findings)}步): "
                f"{json.dumps(findings, ensure_ascii=False, default=str)[:trunc]}\n"
                "请选择下一个工具与参数，或置 done=true 结束。")
            tok_before = llm.tokens_used
            out = llm.complete_structured(SYSTEM, prompt, ExplorationStep)
            tok_delta = llm.tokens_used - tok_before
            if not isinstance(out, dict):
                terminated = "llm_parse_failure"
                audit({"step": i + 1, "error": "structured output unavailable",
                       "tokens": tok_delta})
                break
            if out.get("done"):
                terminated = "agent_done"
                audit({"step": i + 1, "done": True, "reason": out.get("reason", ""),
                       "tokens": tok_delta})
                break
            tool, args = out.get("tool", ""), out.get("args", {}) or {}
            steps += 1
            if tool not in TOOL_SPECS:
                res = {"ok": False, "error": f"tool {tool} not in whitelist",
                       "metrics": {}}
            else:
                res = tools.call(tool, args)
            digest = json.dumps(res.get("result", res.get("error", "")),
                                ensure_ascii=False, default=str)[:trunc]
            audit({"step": steps, "tool": tool, "args": args,
                   "ok": res.get("ok"), "metrics": res.get("metrics", {}),
                   "result_digest": digest, "reason": out.get("reason", ""),
                   "tokens": tok_delta})
            if not res.get("ok"):
                invalid_streak += 1
                findings.append({"tool": tool, "ok": False,
                                 "error": res.get("error", "")[:200]})
                if invalid_streak >= 2:
                    terminated = "consecutive_invalid"
                    break
                continue
            invalid_streak = 0
            _aggregate(agg, res.get("metrics", {}))
            findings.append({"tool": tool, "args": args, "ok": True,
                             "metrics": res.get("metrics", {}),
                             "result": digest[:600]})
            if llm.tokens_used - tokens_start > max_tokens:
                terminated = "token_budget"
                break

    # Oracle 兼容键：error_signal_presence 读 error_count
    agg["error_count"] = agg.get("error_log_count", 0) + agg.get("error_span_count", 0)
    agg["exploration_steps"] = steps
    audit({"summary": True, "action": action, "target": target,
           "steps": steps, "terminated_by": terminated, "metrics": agg,
           "session_tokens": getattr(llm, "tokens_used", 0) - tokens_start,
           "ts": time.time()})
    return agg