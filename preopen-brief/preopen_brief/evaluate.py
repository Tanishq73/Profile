"""Scoring of past signals against realized outcomes, and the evaluation report. Pure."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from .signals import direction

DIRECTIONAL_SIGNALS = ("us_cash", "us_futures", "gift_gap", "breadth_nifty", "breadth_all", "composite_bias")
Z_95 = 1.959963984540054


# --------------------------------------------------------------------------- statistics


def wilson_interval(hits: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion; None when n == 0."""
    if n <= 0:
        return None
    p = hits / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def _ranks(values: list[float]) -> list[float]:
    """Average ranks (ties share the mean rank)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return pearson(_ranks(xs), _ranks(ys))


# --------------------------------------------------------------------------- scoring


def sign_hit(label: str | None, target: float | None) -> bool | None:
    """Did the label call the sign of ``target``? None = not scored
    ("flat"/"mixed" labels and exactly-zero targets are excluded)."""
    call = direction(label)
    if call is None or target is None or target == 0:
        return None
    return (target > 0) == (call > 0)


def score_signals(
    signals: list[dict[str, Any]], realized_gap: float | None, day_return: float | None
) -> list[dict[str, Any]]:
    """Per-signal sign hits (vs opening gap and vs close-to-close return) and,
    for gift_gap, the absolute error of the estimate in percentage points."""
    scores = []
    for s in signals:
        ok = bool(s.get("ok"))
        hit_gap = sign_hit(s.get("label"), realized_gap) if ok else None
        hit_day = sign_hit(s.get("label"), day_return) if ok else None
        abs_error = None
        if ok and s["name"] == "gift_gap" and s.get("value") is not None and realized_gap is not None:
            abs_error = abs(s["value"] - realized_gap)
        scores.append(
            {
                "name": s["name"],
                "hit_gap": None if hit_gap is None else int(hit_gap),
                "hit_day": None if hit_day is None else int(hit_day),
                "abs_error": abs_error,
            }
        )
    return scores


# --------------------------------------------------------------------------- report


def _hit_stats(rows: list[dict[str, Any]], target_key: str) -> dict[str, Any]:
    scored = [(r, sign_hit(r["label"], r[target_key])) for r in rows if r["ok"]]
    scored = [(r, hit) for r, hit in scored if hit is not None]
    n = len(scored)
    hits = sum(1 for _, hit in scored if hit)
    ups = sum(1 for r, _ in scored if r[target_key] > 0)
    interval = wilson_interval(hits, n)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": hits / n if n else None,
        "ci_low": interval[0] if interval else None,
        "ci_high": interval[1] if interval else None,
        "baseline": ups / n if n else None,  # hit rate of "always positive" on the same days
    }


def _gap_stats(rows: list[dict[str, Any]], method: str) -> dict[str, Any]:
    pairs = [
        (r["value"], r["realized_gap"])
        for r in rows
        if r["ok"] and r["value"] is not None and (method == "all" or r["detail"].get("method") == method)
    ]
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    return {
        "method": method,
        "n": len(pairs),
        "mae": sum(abs(x - y) for x, y in pairs) / len(pairs) if pairs else None,
        "pearson": pearson(xs, ys),
        "spearman": spearman(xs, ys),
    }


def suggest_weights(
    signal_stats: dict[str, dict[str, Any]], scored_days: int, cfg: dict[str, Any]
) -> dict[str, Any]:
    """Weight *suggestion* from hit-rate edge over a coin flip. Never applied
    automatically, and refused outright below ``min_days_for_tuning``."""
    minimum = int(cfg["evaluation"]["min_days_for_tuning"])
    if scored_days < minimum:
        return {"status": "not tuned", "reason": f"{scored_days} scored days < {minimum}"}
    breadth = f"breadth_{str(cfg['composite']['breadth_universe']).lower()}"
    edges = {}
    for name in cfg["composite"]["weights"]:
        rate = signal_stats.get(breadth if name == "breadth" else name, {}).get("gap", {}).get("hit_rate")
        edges[name] = max((rate or 0.5) - 0.5, 0.0)
    total = sum(edges.values())
    if total <= 0:
        return {"status": "not tuned", "reason": "no signal beat a coin flip"}
    return {
        "status": "suggestion only (edit config.yaml yourself)",
        "weights": {name: round(edge / total, 2) for name, edge in edges.items()},
    }


def build_report(
    rows: list[dict[str, Any]], total_scored_days: int, days: int, since: date, cfg: dict[str, Any]
) -> dict[str, Any]:
    """``rows`` come from ``Store.evaluation_rows(since)``."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_name.setdefault(row["name"], []).append(row)
    signal_stats = {
        name: {"gap": _hit_stats(by_name.get(name, []), "realized_gap"),
               "day": _hit_stats(by_name.get(name, []), "day_return")}
        for name in DIRECTIONAL_SIGNALS
    }
    gift_rows = by_name.get("gift_gap", [])
    minimum = int(cfg["composite"]["min_scored_days"])
    return {
        "days": days,
        "since": since.isoformat(),
        "window_scored_days": len({r["trade_date"] for r in rows}),
        "total_scored_days": total_scored_days,
        "calibrated": total_scored_days >= minimum,
        "min_scored_days": minimum,
        "signals": signal_stats,
        "gap_est": [_gap_stats(gift_rows, m) for m in ("all", "basis-adjusted", "unadjusted")],
        "weights": suggest_weights(signal_stats, total_scored_days, cfg),
    }


def _pct(value: float | None, width: int = 5) -> str:
    return f"{value * 100:{width}.1f}" if value is not None else " " * (width - 3) + "n/a"


def _num(value: float | None, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if value is not None else "n/a"


def format_report(report: dict[str, Any]) -> str:
    """Fixed-width plain-text report (also sent inside <pre> on Telegram)."""
    status = "calibrated" if report["calibrated"] else (
        f"uncalibrated ({report['total_scored_days']}/{report['min_scored_days']} scored days)"
    )
    lines = [
        f"Evaluation: last {report['days']} days (since {report['since']})",
        f"Scored days in window: {report['window_scored_days']} | {status}",
        "",
        "Sign hit vs opening gap (flat/mixed excluded)",
        f"{'signal':<15}{'n':>4} {'hit%':>5}  {'95% CI':<13} {'base%':>5} {'edge':>6}",
    ]
    for name, stats in report["signals"].items():
        g = stats["gap"]
        if g["n"]:
            ci = f"[{g['ci_low'] * 100:4.1f},{g['ci_high'] * 100:5.1f}]"
            edge = f"{(g['hit_rate'] - g['baseline']) * 100:+6.1f}"
        else:
            ci, edge = "n/a", "   n/a"
        lines.append(f"{name:<15}{g['n']:>4} {_pct(g['hit_rate'])}  {ci:<13} {_pct(g['baseline'])} {edge}")
    lines += ["", "Sign hit vs close-to-close day return", f"{'signal':<15}{'n':>4} {'hit%':>5} {'base%':>5}"]
    for name, stats in report["signals"].items():
        d = stats["day"]
        lines.append(f"{name:<15}{d['n']:>4} {_pct(d['hit_rate'])} {_pct(d['baseline'])}")
    lines += ["", "gap_est vs realized gap (percentage points)"]
    for g in report["gap_est"]:
        lines.append(
            f"{g['method']:<15}n={g['n']:<3} MAE {_num(g['mae'])}  "
            f"Pearson {_num(g['pearson'])}  Spearman {_num(g['spearman'])}"
        )
    weights = report["weights"]
    lines.append("")
    if "weights" in weights:
        pairs = ", ".join(f"{k} {v:.2f}" for k, v in weights["weights"].items())
        lines.append(f"Weights: {weights['status']}: {pairs}")
    else:
        lines.append(f"Weights: {weights['status']} ({weights['reason']})")
    return "\n".join(lines)
