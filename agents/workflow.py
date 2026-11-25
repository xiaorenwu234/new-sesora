"""LangGraph 评估工作流（方案 §4.5 状态机）。

节点：collect -> deterministic -> hypothesis_agent -> select ->
[execute -> select]* -> finalize。
实验选择由自研 EGV 完成，LangGraph 只负责编排与状态传递，
不退化为通用 ReAct Agent。LangGraph 不可用时回退到等价直接循环。
"""
from __future__ import annotations

from typing import TypedDict

from assessment import pipeline


class WorkflowState(TypedDict, total=False):
    ctx: object
    candidate: dict | None
    finalize: bool


def build_workflow():
    from langgraph.graph import StateGraph, END

    def n_collect(state):
        pipeline.step_collect(state["ctx"])
        return {}

    def n_deterministic(state):
        pipeline.step_deterministic(state["ctx"])
        return {}

    def n_hypothesis(state):
        pipeline.step_hypothesis_agent(state["ctx"])
        return {}

    def n_select(state):
        ctx = state["ctx"]
        if ctx.iterations > ctx.budget.max_experiments + 2:
            ctx.stop_reason = "iteration_cap"
            return {"candidate": None}
        best = pipeline.step_select(ctx)
        return {"candidate": best}

    def n_execute(state):
        pipeline.step_execute(state["ctx"], state["candidate"])
        return {"candidate": None}

    def n_finalize(state):
        ctx = state["ctx"]
        if not ctx.stop_reason:
            ctx.stop_reason = "budget_exhausted"
        report = pipeline.step_finalize(ctx)
        return {"report": report}

    def route_select(state):
        return "execute" if state.get("candidate") else "finalize"

    g = StateGraph(WorkflowState)
    g.add_node("collect", n_collect)
    g.add_node("deterministic", n_deterministic)
    g.add_node("hypothesis_agent", n_hypothesis)
    g.add_node("select", n_select)
    g.add_node("execute", n_execute)
    g.add_node("finalize", n_finalize)
    g.set_entry_point("collect")
    g.add_edge("collect", "deterministic")
    g.add_edge("deterministic", "hypothesis_agent")
    g.add_edge("hypothesis_agent", "select")
    g.add_conditional_edges("select", route_select,
                            {"execute": "execute", "finalize": "finalize"})
    g.add_edge("execute", "select")
    g.add_edge("finalize", END)
    return g.compile()


def run(ctx) -> dict:
    try:
        app = build_workflow()
        final = app.invoke({"ctx": ctx}, config={"recursion_limit": 100})
        return final.get("report") or pipeline.step_finalize(ctx)
    except ImportError:
        return pipeline.run_loop(ctx)