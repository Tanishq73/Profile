"""Deterministic signals. Pure functions: inputs in, ``Signal`` out, no I/O.

All percentages are in percent units. Thresholds come from ``config.yaml``
(the ``signals`` section) and are passed in; nothing is hard-coded here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any

POSITIVE, NEGATIVE, FLAT = "positive", "negative", "flat"
STRONG_POSITIVE, STRONG_NEGATIVE = "strong positive", "strong negative"


@dataclass
class Signal:
    name: str
    ok: bool
    label: str | None = None       # direction label, or VIX regime
    value: float | None = None     # headline number (percent, or VIX level)
    reason: str | None = None      # why it failed (shown as "n/a (<reason>)")
    flags: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def failed(name: str, reason: str, **detail: Any) -> Signal:
    return Signal(name=name, ok=False, reason=reason, detail=detail)


def direction(label: str | None) -> int | None:
    """+1 / -1 for a directional label; None for flat, mixed, regimes or missing."""
    if label in (POSITIVE, STRONG_POSITIVE):
        return 1
    if label in (NEGATIVE, STRONG_NEGATIVE):
        return -1
    return None


def band_label(value: float, flat_band: float, strong_band: float | None = None) -> str:
    """|value| < flat_band -> flat; >= strong_band -> strong; else plain direction."""
    if abs(value) < flat_band:
        return FLAT
    if strong_band is not None and abs(value) >= strong_band:
        return STRONG_POSITIVE if value > 0 else STRONG_NEGATIVE
    return POSITIVE if value > 0 else NEGATIVE


# --------------------------------------------------------------------------- US


def mean_change_signal(
    name: str,
    changes: dict[str, float | None],
    cfg: dict[str, Any],
    missing_reasons: dict[str, str] | None = None,
) -> Signal:
    """Mean % change across components (us_cash, us_futures).

    ``changes`` maps display name -> % change or None. Components that are
    missing are skipped and flagged; fewer than ``min_components`` fails.
    """
    available = {k: v for k, v in changes.items() if v is not None}
    missing = [k for k in changes if k not in available]
    detail = {"components": changes}
    if missing_reasons:
        detail["missing_reasons"] = missing_reasons
    if len(available) < int(cfg["min_components"]):
        reasons = sorted(set((missing_reasons or {}).values()))
        reason = reasons[0] if len(reasons) == 1 else f"only {len(available)}/{len(changes)} components"
        return Signal(name=name, ok=False, reason=reason, detail=detail)
    mean = sum(available.values()) / len(available)
    flags = [f"partial: no {', '.join(missing)}"] if missing else []
    return Signal(
        name=name,
        ok=True,
        label=band_label(mean, float(cfg["flat_band_pct"])),
        value=mean,
        flags=flags,
        detail=detail,
    )


# --------------------------------------------------------------------------- GIFT


def gift_gap_signal(
    gift: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    nifty_prev_close: float | None,
    now: datetime,
    cfg: dict[str, Any],
    *,
    gift_error: str | None = None,
    snapshot_problem: str | None = None,
) -> Signal:
    """Estimated opening gap from GIFT Nifty.

    Primary (basis-adjusted): (GIFT_now - GIFT_at_prev_close_snapshot) / NIFTY_prev_close,
    using the previous trading day's 15:32 snapshot. Invalid when the snapshot is
    missing/unusable (``snapshot_problem``) or EXPIRYDATE differs (contract roll).
    Fallback: GIFT PERCHANGE, flagged "unadjusted".
    ``raw_spread`` is diagnostic only: it still contains the futures basis and is
    never used for the label.
    """
    name = "gift_gap"
    if gift is None:
        return failed(name, gift_error or "GIFT quote missing")
    ts = gift.get("timestamp")
    if ts is None:
        return failed(name, "GIFT timestamp missing", last=gift.get("last"))
    age_min = (now - ts).total_seconds() / 60.0
    max_age = float(cfg["max_quote_age_minutes"])
    if age_min > max_age:
        return failed(
            name,
            f"stale GIFT quote, {ts:%H:%M} IST",
            last=gift["last"],
            quote_age_minutes=round(age_min, 1),
        )

    last = gift["last"]
    detail: dict[str, Any] = {
        "last": last,
        "expiry": gift.get("expiry"),
        "timestamp": ts.isoformat(),
        "quote_age_minutes": round(age_min, 1),
        "perchange": gift.get("pct_change"),
        "raw_spread_pct": None,
    }
    if nifty_prev_close:
        detail["nifty_prev_close"] = nifty_prev_close
        detail["raw_spread_pct"] = (last - nifty_prev_close) / nifty_prev_close * 100.0

    problem = snapshot_problem
    if problem is None and snapshot is None:
        problem = "no prior close snapshot"
    if problem is None and (not snapshot.get("gift_last") or not snapshot.get("nifty_close")):
        problem = "prior snapshot incomplete"
    if problem is None and not snapshot.get("gift_fresh", True):
        problem = "prior snapshot GIFT was stale"
    if problem is None and (not gift.get("expiry") or snapshot.get("gift_expiry") != gift.get("expiry")):
        problem = "contract roll"

    if problem is None:
        gap = (last - snapshot["gift_last"]) / snapshot["nifty_close"] * 100.0
        method = "basis-adjusted"
        detail.update(
            snapshot_gift=snapshot["gift_last"],
            snapshot_nifty_close=snapshot["nifty_close"],
            snapshot_date=str(snapshot.get("trade_date")),
        )
    elif gift.get("pct_change") is not None:
        gap = float(gift["pct_change"])
        method = "unadjusted"
        detail["fallback_reason"] = problem
    else:
        return Signal(name=name, ok=False, reason=f"{problem}; PERCHANGE missing", detail=detail)

    detail["method"] = method
    return Signal(
        name=name,
        ok=True,
        label=band_label(gap, float(cfg["flat_band_pct"]), float(cfg["strong_band_pct"])),
        value=gap,
        flags=[method],
        detail=detail,
    )


def resolve_nifty_prev_close(
    today: date,
    capital_market: dict[str, Any] | None,
    nifty_index: dict[str, Any] | None,
    all_indices_ts: datetime | None,
    yahoo_bars: list[dict[str, Any]] | None,
) -> tuple[float | None, str | None]:
    """Best available NIFTY 50 previous close and where it came from.

    Before the open NSE still serves yesterday's session, where ``last`` *is*
    the previous close; once today's session is live, ``previousClose`` is. The
    payload's own date decides which.
    """
    if nifty_index and all_indices_ts is not None:
        key = "last" if all_indices_ts.date() < today else "previous_close"
        if nifty_index.get(key):
            return nifty_index[key], f"allIndices.{key}"
    if capital_market and capital_market.get("trade_date") is not None:
        key = "last" if capital_market["trade_date"].date() < today else "prev_close"
        if capital_market.get(key):
            return capital_market[key], f"marketStatus.{key}"
    if yahoo_bars:
        prior = [b for b in yahoo_bars if date.fromisoformat(b["t"]) < today]
        if prior:
            return prior[-1]["c"], "yahoo ^NSEI"
    return None, None


# --------------------------------------------------------------------------- VIX


def vix_regime(level: float, cfg: dict[str, Any]) -> str:
    if level < float(cfg["calm_below"]):
        return "calm"
    if level < float(cfg["stable_below"]):
        return "stable"
    if level <= float(cfg["elevated_upto"]):
        return "elevated"
    return "high"


def percentile_rank(level: float, history: list[float]) -> float:
    """Share of ``history`` at or below ``level``, 0-100."""
    return 100.0 * sum(1 for h in history if h <= level) / len(history)


def vix_signal(
    level: float | None,
    change_pct: float | None,
    history: list[float],
    fetched_at: dtime,
    cfg: dict[str, Any],
    *,
    source: str = "nse",
    error: str | None = None,
) -> Signal:
    """India VIX level, day change and percentile vs the trailing window.

    Magnitude only: this signal carries no direction and never enters the bias.
    """
    name = "vix"
    if level is None or level <= 0:
        return failed(name, error or "India VIX missing")
    window = history[-int(cfg["percentile_window"]):]
    percentile = percentile_rank(level, window) if len(window) >= int(cfg["min_history"]) else None
    flags = []
    if fetched_at < _hhmm(cfg["prev_close_before"]):
        flags.append("prev close")
    if source != "nse":
        flags.append(source)
    return Signal(
        name=name,
        ok=True,
        label=vix_regime(level, cfg),
        value=level,
        flags=flags,
        detail={
            "change_pct": change_pct,
            "percentile": percentile,
            "history_sessions": len(window),
            "source": source,
        },
    )


# --------------------------------------------------------------------------- breadth


def breadth_signal(
    universe: str,
    preopen: dict[str, Any] | None,
    fetched_at: dtime,
    cfg: dict[str, Any],
    *,
    error: str | None = None,
) -> Signal:
    """ad_ratio = adv / max(dec, 1); breadth = (adv - dec) / (adv + dec + unch)."""
    name = f"breadth_{universe.lower()}"
    if preopen is None:
        return failed(name, error or "pre-open data missing")
    adv, dec, unch = preopen["advances"], preopen["declines"], preopen["unchanged"]
    total = adv + dec + unch
    if total <= 0:
        return failed(name, "no stocks in pre-open data")
    breadth = (adv - dec) / total
    band = float(cfg["band"])
    label = FLAT if abs(breadth) < band else (POSITIVE if breadth > 0 else NEGATIVE)
    flags = []
    if fetched_at < _hhmm(cfg["indicative_before"]):
        flags.append("indicative")
    if preopen.get("derived_counts"):
        flags.append("counts derived from pChange")
    return Signal(
        name=name,
        ok=True,
        label=label,
        value=breadth,
        flags=flags,
        detail={
            "advances": adv,
            "declines": dec,
            "unchanged": unch,
            "ad_ratio": adv / max(dec, 1),
            "universe": universe,
        },
    )


def _hhmm(value: str) -> dtime:
    hh, mm = str(value).split(":")
    return dtime(int(hh), int(mm))
