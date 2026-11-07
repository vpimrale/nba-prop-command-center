"""Calibration metrics: Brier, log loss, reliability bins with Wilson intervals, PIT histogram."""
from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def brier(ps: list[float], ys: list[int]) -> float | None:
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps) if ps else None


def log_loss(ps: list[float], ys: list[int], eps: float = 1e-4) -> float | None:
    if not ps:
        return None
    return -sum(y * math.log(min(1 - eps, max(eps, p))) + (1 - y) * math.log(min(1 - eps, max(eps, 1 - p))) for p, y in zip(ps, ys)) / len(ps)


def reliability(ps: list[float], ys: list[int], bins: int = 10) -> list[dict]:
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(ps) if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if not idx:
            continue
        k = sum(ys[i] for i in idx)
        n = len(idx)
        ci = wilson(k, n)
        out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": n, "mean_pred": round(sum(ps[i] for i in idx) / n, 4), "observed": round(k / n, 4),
                    "ci95": [round(ci[0], 4), round(ci[1], 4)]})
    return out


def ece(rel: list[dict]) -> float | None:
    n = sum(r["n"] for r in rel)
    return round(sum(r["n"] * abs(r["mean_pred"] - r["observed"]) for r in rel) / n, 4) if n else None


def pit_hist(pits: list[float], bins: int = 10) -> list[int]:
    h = [0] * bins
    for v in pits:
        h[min(bins - 1, int(v * bins))] += 1
    return h


def summarize_binary(ps: list[float], ys: list[int]) -> dict:
    rel = reliability(ps, ys)
    base = sum(ys) / len(ys) if ys else None
    return {"n": len(ps), "base_rate": round(base, 4) if base is not None else None, "brier": round(brier(ps, ys), 5) if ps else None,
            "brier_constant_baseline": round(brier([base] * len(ys), ys), 5) if ys else None, "log_loss": round(log_loss(ps, ys), 5) if ps else None,
            "ece": ece(rel), "reliability": rel}
