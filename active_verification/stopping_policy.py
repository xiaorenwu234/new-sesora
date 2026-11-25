"""停止策略：预算耗尽 / 无未决假设 / 最优效用非正。"""
from __future__ import annotations

from common.models import Budget


def should_stop(budget: Budget, unresolved: list, best_utility: float | None) -> str | None:
    if not budget.available():
        return "budget_exhausted"
    if not unresolved:
        return "all_resolved"
    if best_utility is not None and best_utility <= 0:
        return "no_positive_utility_action"
    return None