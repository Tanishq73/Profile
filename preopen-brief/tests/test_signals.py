"""Signal functions: bands, edge cases, and the composite."""

from datetime import date, time, timedelta

import pytest

from preopen_brief import signals as sg
from preopen_brief.composite import composite_bias
from preopen_brief.signals import Signal

from .conftest import MORNING_NOW, ist

GIFT = {"last": 26030.0, "pct_change": 0.30, "expiry": "2026-11-26", "timestamp": ist("2026-11-13T09:08:00")}
SNAP = {"trade_date": "2026-11-12", "gift_last": 25948.0, "gift_expiry": "2026-11-26",
        "nifty_close": 25875.80, "gift_fresh": True}


# -- bands ---------------------------------------------------------------------

@pytest.mark.parametrize("value,label", [
    (0.31, "positive"), (0.30, "positive"), (0.29, "flat"), (0.0, "flat"),
    (-0.29, "flat"), (-0.30, "negative"), (-2.0, "negative"),
])
def test_us_band_is_plus_minus_point_three(cfg, value, label):
    s = sg.mean_change_signal("us_cash", {"S&P": value, "Nasdaq": value, "Dow": value}, cfg["signals"]["us_cash"])
    assert s.ok and s.label == label and s.value == pytest.approx(value)


def test_us_mean_with_missing_ticker_is_partial(cfg):
    s = sg.mean_change_signal("us_cash", {"S&P": 0.5, "Nasdaq": 0.7, "Dow": None}, cfg["signals"]["us_cash"])
    assert s.ok and s.value == pytest.approx(0.6) and s.flags == ["partial: no Dow"]


def test_us_too_few_components_fails_with_reason(cfg):
    s = sg.mean_change_signal("us_futures", {"ES": 0.5, "NQ": None, "YM": None}, cfg["signals"]["us_futures"],
                              {"NQ": "yahoo: rate limited", "YM": "yahoo: rate limited"})
    assert not s.ok and s.reason == "yahoo: rate limited"


# -- gift_gap --------------------------------------------------------------------

def gift(cfg, gift=GIFT, snap=SNAP, prev=25875.80, now=MORNING_NOW, **kw):
    return sg.gift_gap_signal(gift, snap, prev, now, cfg["signals"]["gift_gap"], **kw)


def test_gift_basis_adjusted_primary(cfg):
    s = gift(cfg)
    assert s.ok and s.flags == ["basis-adjusted"]
    assert s.value == pytest.approx((26030.0 - 25948.0) / 25875.80 * 100)  # 0.317 %
    assert s.label == "positive"
    # raw spread still contains the basis: diagnostic only, never the label
    assert s.detail["raw_spread_pct"] == pytest.approx((26030.0 - 25875.80) / 25875.80 * 100)
    assert s.detail["raw_spread_pct"] > s.value


def test_gift_contract_roll_falls_back_to_unadjusted(cfg):
    s = gift(cfg, snap={**SNAP, "gift_expiry": "2026-10-29"})
    assert s.ok and s.flags == ["unadjusted"] and s.value == 0.30
    assert s.detail["fallback_reason"] == "contract roll"


def test_gift_missing_snapshot_falls_back_to_unadjusted(cfg):
    s = gift(cfg, snap=None)
    assert s.ok and s.flags == ["unadjusted"] and s.detail["fallback_reason"] == "no prior close snapshot"
    s = gift(cfg, snapshot_problem="no snapshot for previous session (latest 09 Nov)")
    assert s.flags == ["unadjusted"]
    s = gift(cfg, snap={**SNAP, "gift_fresh": False})
    assert s.flags == ["unadjusted"] and "stale" in s.detail["fallback_reason"]


def test_gift_stale_timestamp_is_rejected(cfg):
    fresh = gift(cfg, gift={**GIFT, "timestamp": MORNING_NOW - timedelta(minutes=15)})
    assert fresh.ok
    stale = gift(cfg, gift={**GIFT, "timestamp": MORNING_NOW - timedelta(minutes=15, seconds=1)})
    assert not stale.ok and stale.reason == "stale GIFT quote, 08:54 IST"
    assert not gift(cfg, gift={**GIFT, "timestamp": None}).ok


def test_gift_missing_quote_and_missing_fallback(cfg):
    s = gift(cfg, gift=None, gift_error="NSE marketStatus: HTTP 403 after cookie refresh")
    assert not s.ok and s.reason == "NSE marketStatus: HTTP 403 after cookie refresh"
    s = gift(cfg, gift={**GIFT, "pct_change": None}, snap=None)
    assert not s.ok and "PERCHANGE missing" in s.reason


@pytest.mark.parametrize("gap_pct,label", [
    (0.24, "flat"), (0.25, "positive"), (0.74, "positive"), (0.75, "strong positive"),
    (-0.24, "flat"), (-0.25, "negative"), (-0.75, "strong negative"),
])
def test_gift_bands(cfg, gap_pct, label):
    s = gift(cfg, gift={**GIFT, "pct_change": gap_pct}, snap=None)
    assert s.label == label


def test_resolve_nifty_prev_close_depends_on_payload_date():
    today = date(2026, 11, 13)
    row = {"last": 25875.8, "previous_close": 25763.45}
    # before the open NSE still serves yesterday: "last" is the previous close
    assert sg.resolve_nifty_prev_close(today, None, row, ist("2026-11-12T15:30:00"), None) == (25875.8, "allIndices.last")
    # once today's session is live, previousClose is
    live = {"last": 25968.4, "previous_close": 25875.8}
    assert sg.resolve_nifty_prev_close(today, None, live, ist("2026-11-13T09:20:00"), None) == (25875.8, "allIndices.previous_close")
    bars = [{"t": "2026-11-12", "c": 25875.8}, {"t": "2026-11-13", "c": 25968.4}]
    assert sg.resolve_nifty_prev_close(today, None, None, None, bars) == (25875.8, "yahoo ^NSEI")
    assert sg.resolve_nifty_prev_close(today, None, None, None, None) == (None, None)


# -- vix -------------------------------------------------------------------------

@pytest.mark.parametrize("level,regime", [
    (11.99, "calm"), (12.0, "stable"), (14.99, "stable"), (15.0, "elevated"), (20.0, "elevated"), (20.01, "high"),
])
def test_vix_regimes(cfg, level, regime):
    assert sg.vix_regime(level, cfg["signals"]["vix"]) == regime


def test_vix_percentile_and_prev_close_label(cfg):
    history = [10 + i * 0.05 for i in range(300)]  # only the trailing 252 are used
    s = sg.vix_signal(13.42, -1.18, history, time(9, 9), cfg["signals"]["vix"])
    window = history[-252:]
    assert s.detail["percentile"] == pytest.approx(100 * sum(1 for h in window if h <= 13.42) / 252)
    assert s.detail["history_sessions"] == 252
    assert s.flags == ["prev close"] and s.label == "stable"
    assert sg.vix_signal(13.42, -1.18, history, time(9, 15), cfg["signals"]["vix"]).flags == []


def test_vix_without_history_or_level(cfg):
    s = sg.vix_signal(13.42, None, [12.0] * 10, time(9, 9), cfg["signals"]["vix"])
    assert s.ok and s.detail["percentile"] is None
    assert not sg.vix_signal(None, None, [], time(9, 9), cfg["signals"]["vix"], error="NSE allIndices: HTTP 403").ok


# -- breadth ---------------------------------------------------------------------

def breadth(cfg, adv, dec, unch, at=time(9, 9)):
    return sg.breadth_signal("NIFTY", {"advances": adv, "declines": dec, "unchanged": unch}, at, cfg["signals"]["breadth"])


def test_breadth_zero_declines_does_not_divide_by_zero(cfg):
    s = breadth(cfg, 50, 0, 0)
    assert s.detail["ad_ratio"] == 50 and s.value == 1.0 and s.label == "positive"
    assert breadth(cfg, 0, 0, 50).label == "flat"
    assert not breadth(cfg, 0, 0, 0).ok


@pytest.mark.parametrize("adv,dec,unch,label", [
    (31, 17, 2, "positive"),   # +0.28
    (30, 20, 0, "positive"),   # +0.20 exactly
    (29, 20, 1, "flat"),       # +0.18
    (20, 30, 0, "negative"),
    (1040, 890, 95, "flat"),   # +0.074
])
def test_breadth_bands(cfg, adv, dec, unch, label):
    s = breadth(cfg, adv, dec, unch)
    assert s.label == label and s.value == pytest.approx((adv - dec) / (adv + dec + unch))


def test_breadth_indicative_before_0908(cfg):
    assert breadth(cfg, 31, 17, 2, at=time(9, 7, 59)).flags == ["indicative"]
    assert breadth(cfg, 31, 17, 2, at=time(9, 8)).flags == []
    assert not sg.breadth_signal("ALL", None, time(9, 9), cfg["signals"]["breadth"], error="NSE pre-open ALL: HTTP 403").ok


# -- composite -------------------------------------------------------------------

def sig(name, label, ok=True):
    return Signal(name=name, ok=ok, label=label if ok else None, value=0.0, reason=None if ok else "down")


def all_signals(us_cash="positive", us_futures="positive", gift_gap="positive", breadth="positive", vix="stable"):
    return {
        "us_cash": sig("us_cash", us_cash), "us_futures": sig("us_futures", us_futures),
        "gift_gap": sig("gift_gap", gift_gap), "vix": sig("vix", vix),
        "breadth_nifty": sig("breadth_nifty", breadth), "breadth_all": sig("breadth_all", "flat"),
    }


def test_composite_labels_and_completeness(cfg):
    c = composite_bias(all_signals(), cfg["composite"], scored_days=0)
    assert c.bias == "positive" and c.score == pytest.approx(1.0)
    assert (c.sources_ok, c.sources_total, c.data_completeness) == (6, 6, 1.0)
    assert not c.calibrated
    assert composite_bias(all_signals("negative", "negative", "strong negative", "negative"), cfg["composite"], 0).bias == "negative"
    assert composite_bias(all_signals("positive", "negative", "flat", "flat"), cfg["composite"], 0).bias == "mixed"


def test_composite_ignores_vix_for_direction(cfg):
    calm = composite_bias(all_signals(vix="calm"), cfg["composite"], 0)
    high = composite_bias(all_signals(vix="high"), cfg["composite"], 0)
    assert calm.score == high.score and calm.bias == high.bias and "vix" not in high.contributions
    assert calm.confidence_note is None and high.confidence_note == "VIX high: lower confidence"
    cfg["composite"]["weights"]["vix"] = 0.5
    with pytest.raises(ValueError, match="magnitude"):
        composite_bias(all_signals(), cfg["composite"], 0)


def test_composite_with_partial_data_renormalises(cfg):
    signals = all_signals(us_cash="negative")
    signals["gift_gap"] = sig("gift_gap", None, ok=False)
    signals["breadth_nifty"] = sig("breadth_nifty", None, ok=False)   # falls back to breadth_all (flat)
    c = composite_bias(signals, cfg["composite"], 0)
    w = cfg["composite"]["weights"]
    expected = (-w["us_cash"] + w["us_futures"]) / (w["us_cash"] + w["us_futures"] + w["breadth"])
    assert c.score == pytest.approx(expected)
    assert (c.sources_ok, c.sources_total) == (4, 6)


def test_composite_with_no_inputs_is_mixed(cfg):
    signals = {name: sig(name, None, ok=False) for name in all_signals()}
    c = composite_bias(signals, cfg["composite"], 0)
    assert c.bias == "mixed" and c.score is None and c.sources_ok == 0


def test_composite_uncalibrated_until_sixty_scored_days(cfg):
    assert not composite_bias(all_signals(), cfg["composite"], 59).calibrated
    assert composite_bias(all_signals(), cfg["composite"], 60).calibrated
