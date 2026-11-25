"""统计分析（RQ1-RQ4）：重复运行的置信区间与显著性检验。

策略对比实验需多次独立重复（不同 seed），报告均值 + 95% bootstrap CI；
组间比较用 Mann-Whitney U（非正态小样本）+ Holm 多重比较校正。
"""
from __future__ import annotations

import random
import statistics


def bootstrap_ci(values: list[float], n_boot: int = 2000, alpha: float = 0.05,
                 seed: int = 0) -> dict:
    if not values:
        return {"mean": None, "ci_low": None, "ci_high": None, "n": 0}
    rng = random.Random(seed)
    means = []
    n = len(values)
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.mean(sample))
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[int((1 - alpha / 2) * n_boot) - 1]
    return {"mean": round(statistics.mean(values), 4),
            "ci_low": round(lo, 4), "ci_high": round(hi, 4), "n": n}


def mann_whitney_u(a: list[float], b: list[float]) -> dict:
    """双侧 Mann-Whitney U（正态近似，含并列校正）。"""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return {"u": None, "p_value": None}
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks: dict[float, float] = {}
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        ranks[combined[i][0]] = avg_rank
        i = j + 1
    r1 = sum(ranks[v] for v in a)
    u1 = r1 - n1 * (n1 + 1) / 2
    u = min(u1, n1 * n2 - u1)
    mu = n1 * n2 / 2
    # 并列校正
    tie_term = 0.0
    counts: dict[float, int] = {}
    for v, _ in combined:
        counts[v] = counts.get(v, 0) + 1
    n = n1 + n2
    for t in counts.values():
        if t > 1:
            tie_term += t ** 3 - t
    sigma = ((n1 * n2 / 12) * ((n + 1) - tie_term / (n * (n - 1)))) ** 0.5
    if sigma == 0:
        return {"u": u, "p_value": 1.0}
    z = (u - mu) / sigma
    # 双侧 p（正态近似，erfc）
    import math
    p = math.erfc(abs(z) / math.sqrt(2))
    return {"u": u, "z": round(z, 4), "p_value": round(p, 6)}


def holm_correction(p_values: list[float]) -> list[float]:
    """Holm-Bonferroni 多重比较校正，返回校正后 p（保持原顺序）。"""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        val = (m - rank) * p_values[idx]
        running = max(running, val)
        adjusted[idx] = min(running, 1.0)
    return adjusted