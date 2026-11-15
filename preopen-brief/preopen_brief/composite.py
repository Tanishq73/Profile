"""Composite directional bias. Pure; weights and thresholds from config.yaml."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .signals import STRONG_NEGATIVE, STRONG_POSITIVE, Signal, direction

MIXED = "mixed"

# Sources counted for data_completeness (one per line of the brief).
COMPLETENESS_SIGNALS = ("us_cash", "us_futures", "gift_gap", "vix", "breadth_nifty", "breadth_all")


@dataclass
class Composite:
    bias: str                      # positive | negative | mixed
    score: float | None            # weighted score in roughly [-1.5, 1.5]; None if no inputs
    sources_ok: int
    sources_total: int
    calibrated: bool
    scored_days: int
    contributions: dict[str, float] = field(default_factory=dict)
    confidence_note: str | None = None

    @property
    def data_completeness(self) -> float:
        return self.sources_ok / self.sources_total if self.sources_total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "data_completeness": self.data_completeness}


def signal_score(signal: Signal, strong_score: float) -> float:
    """Directional score of one signal: +-1, +-strong_score for strong labels, 0 for flat."""
    sign = direction(signal.label)
    if sign is None:
        return 0.0
    magnitude = strong_score if signal.label in (STRONG_POSITIVE, STRONG_NEGATIVE) else 1.0
    return sign * magnitude


def composite_bias(signals: dict[str, Signal], cfg: dict[str, Any], scored_days: int) -> Composite:
    """Weighted sum of us_cash, us_futures, gift_gap and breadth.

    Weights are renormalised over the signals that are available. VIX is never
    an input: it only produces ``confidence_note``.
    """
    weights: dict[str, float] = cfg["weights"]
    if "vix" in weights:
        raise ValueError("vix must not be a composite input: it is a magnitude measure")

    primary = f"breadth_{str(cfg['breadth_universe']).lower()}"
    other = "breadth_all" if primary == "breadth_nifty" else "breadth_nifty"
    breadth = signals.get(primary)
    if breadth is None or not breadth.ok:
        breadth = signals.get(other)
    inputs = {name: signals.get(name) for name in weights if name != "breadth"}
    if "breadth" in weights:
        inputs["breadth"] = breadth

    contributions: dict[str, float] = {}
    weight_sum = 0.0
    for name, signal in inputs.items():
        if signal is None or not signal.ok:
            continue
        weight = float(weights[name])
        contributions[name] = weight * signal_score(signal, float(cfg["strong_score"]))
        weight_sum += weight

    if weight_sum > 0:
        score = sum(contributions.values()) / weight_sum
        threshold = float(cfg["bias_threshold"])
        bias = "positive" if score >= threshold else "negative" if score <= -threshold else MIXED
    else:
        score, bias = None, MIXED

    counted = [signals.get(name) for name in COMPLETENESS_SIGNALS]
    vix = signals.get("vix")
    note = None
    if vix is not None and vix.ok and vix.label in ("elevated", "high"):
        note = f"VIX {vix.label}: lower confidence"
    return Composite(
        bias=bias,
        score=score,
        sources_ok=sum(1 for s in counted if s is not None and s.ok),
        sources_total=len(counted),
        calibrated=scored_days >= int(cfg["min_scored_days"]),
        scored_days=scored_days,
        contributions=contributions,
        confidence_note=note,
    )
