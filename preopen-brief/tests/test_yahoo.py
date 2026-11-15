"""Yahoo helpers: frame conversion, missing tickers, US DST handling, retry."""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from preopen_brief.retry import SourceError
from preopen_brief.sources import yahoo

from .conftest import MORNING_NOW, ist, load_fixture

UTC = timezone.utc


def daily_frame(data: dict[str, list[float]], days: list[str]) -> pd.DataFrame:
    columns = pd.MultiIndex.from_product([list(data), ["Open", "High", "Low", "Close", "Volume"]])
    frame = pd.DataFrame(index=pd.to_datetime(days), columns=columns, dtype=float)
    for ticker, closes in data.items():
        for field in ("Open", "High", "Low", "Close"):
            frame[(ticker, field)] = closes
    return frame


def test_frame_to_bars_and_missing_ticker():
    nan = float("nan")
    frame = daily_frame({"^GSPC": [100.0, 101.0, 102.0], "^IXIC": [200.0, nan, 204.0], "^DJI": [nan, nan, nan]},
                        ["2026-11-10", "2026-11-11", "2026-11-12"])
    bars = yahoo.frame_to_bars(frame, ["^GSPC", "^IXIC", "^DJI", "ES=F"], intraday=False)
    assert [b["t"] for b in bars["^GSPC"]] == ["2026-11-10", "2026-11-11", "2026-11-12"]
    assert [b["c"] for b in bars["^IXIC"]] == [200.0, 204.0]   # NaN row dropped
    assert bars["^DJI"] == []                                   # all-NaN: Yahoo returned nothing
    assert bars["ES=F"] == []                                   # column absent entirely
    assert yahoo.daily_change(bars["^GSPC"])["pct"] == pytest.approx(100 * (102 / 101 - 1))
    assert yahoo.daily_change(bars["^DJI"]) is None


def test_intraday_frame_is_normalised_to_utc():
    index = pd.DatetimeIndex(["2026-11-12 15:00", "2026-11-12 16:00"]).tz_localize("America/New_York")
    frame = pd.DataFrame({("ES=F", "Close"): [7369.5, 7371.0]}, index=index)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    bars = yahoo.frame_to_bars(frame, ["ES=F"], intraday=True)["ES=F"]
    assert bars[0]["t"] == "2026-11-12T20:00:00+00:00"  # 15:00 EST


def test_empty_frame_gives_empty_lists():
    assert yahoo.frame_to_bars(pd.DataFrame(), ["^GSPC"], intraday=False) == {"^GSPC": []}


# -- US DST: India never shifts, New York does ---------------------------------

@pytest.mark.parametrize("ist_now,expected_utc", [
    ("2026-07-15T09:09:00", "2026-07-14T20:00:00"),  # summer: 16:00 EDT = 20:00 UTC
    ("2026-12-15T09:09:00", "2026-12-14T21:00:00"),  # winter: 16:00 EST = 21:00 UTC
    ("2026-03-09T09:09:00", "2026-03-06T21:00:00"),  # Mon after US spring-forward -> Friday, still EST
    ("2026-03-10T09:09:00", "2026-03-09T20:00:00"),  # first EDT session
    ("2026-11-02T09:09:00", "2026-10-30T20:00:00"),  # Mon after US fall-back -> Friday, still EDT
    ("2026-11-03T09:09:00", "2026-11-02T21:00:00"),  # first EST session
    ("2026-11-13T15:32:00", "2026-11-12T21:00:00"),  # close job: US not yet open, same reference
    ("2026-07-15T01:45:00", "2026-07-14T20:00:00"),  # 16:15 EDT: today's close has just passed
    ("2026-12-15T01:45:00", "2026-12-11T21:00:00"),  # 15:15 EST Monday: not yet -> Friday's close
])
def test_us_cash_close_reference_across_dst(ist_now, expected_utc):
    assert yahoo.us_cash_close_reference(ist(ist_now)) == datetime.fromisoformat(expected_utc).replace(tzinfo=UTC)


def hourly(start: datetime, closes: list[float]) -> list[dict]:
    return [{"t": (start + timedelta(hours=i)).isoformat(), "c": c} for i, c in enumerate(closes)]


def test_futures_change_uses_the_bar_ending_at_the_cash_close_in_summer_and_winter():
    # Winter: the 20:00 UTC bar (15:00 EST) closes at the cash close.
    winter = hourly(datetime(2026, 12, 14, 19, tzinfo=UTC), [100, 200, 201, 202, 203, 204, 205, 206, 207])
    got = yahoo.futures_change(winter, ist("2026-12-15T09:09:00"), 120)
    assert got["reference"] == 200 and got["last"] == 207
    # Summer: one hour earlier in UTC - the 19:00 UTC bar (15:00 EDT).
    summer = hourly(datetime(2026, 7, 14, 19, tzinfo=UTC), [100, 200, 201, 202, 203, 204, 205, 206, 207])
    got = yahoo.futures_change(summer, ist("2026-07-15T09:09:00"), 120)
    assert got["reference"] == 100 and got["last"] == 207
    assert got["pct"] == pytest.approx(107.0)


def test_futures_change_from_fixture():
    payload = load_fixture("morning", "yahoo")
    got = yahoo.futures_change(yahoo.bars_for(payload, "ES=F", "intraday"), MORNING_NOW, 120)
    assert got["pct"] == pytest.approx(0.12, abs=0.001)
    assert got["reference_time"] == "2026-11-12T21:00:00+00:00"


def test_futures_change_rejects_stale_and_empty():
    bars = yahoo.bars_for(load_fixture("morning", "yahoo"), "ES=F", "intraday")
    with pytest.raises(SourceError, match="stale quote"):
        yahoo.futures_change(bars, MORNING_NOW + timedelta(hours=3), 120)
    with pytest.raises(SourceError, match="no intraday bars"):
        yahoo.futures_change([], MORNING_NOW, 120)
    only_before = [b for b in bars if b["t"] < "2026-11-12T21"]
    with pytest.raises(SourceError, match="no trade since US cash close"):
        yahoo.futures_change(only_before, MORNING_NOW, 120)


def test_us_session_in_progress():
    assert yahoo.us_session_in_progress(date(2026, 11, 12), ist("2026-11-12T22:00:00"))      # 11:30 EST
    assert not yahoo.us_session_in_progress(date(2026, 11, 12), ist("2026-11-13T09:09:00"))  # closed


# -- fetch: batching + retry ---------------------------------------------------

def test_fetch_makes_two_batched_calls_and_retries_with_backoff(cfg):
    calls, sleeps = [], []
    frame = daily_frame({t: [1.0, 2.0] for t in yahoo.all_daily_tickers(cfg)}, ["2026-11-11", "2026-11-12"])

    def download(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("YFRateLimitError")
        return frame if kwargs["interval"] == "1d" else pd.DataFrame()

    payload = yahoo.YahooSource(cfg, download=download, sleep=sleeps.append, rng=lambda: 0.0).fetch()
    daily_calls = [c for c in calls if c["interval"] == "1d"]
    assert len(daily_calls) == 2 and len(sleeps) >= 1            # one retry after the rate limit
    assert sorted(daily_calls[0]["tickers"]) == sorted(yahoo.all_daily_tickers(cfg))  # one batch, all tickers
    assert len(payload["daily"]["^GSPC"]) == 2
    assert "intraday" in payload["errors"]                        # hourly call came back empty


def test_fetch_single_call_in_daily_mode(cfg):
    cfg["yahoo"]["futures_reference"] = "daily"
    calls = []
    frame = daily_frame({t: [1.0, 2.0] for t in yahoo.all_daily_tickers(cfg)}, ["2026-11-11", "2026-11-12"])
    yahoo.YahooSource(cfg, download=lambda **kw: calls.append(kw) or frame).fetch()
    assert len(calls) == 1


def test_fetch_total_failure_raises(cfg):
    def download(**kwargs):
        raise ConnectionError("down")

    with pytest.raises(SourceError, match="yfinance: ConnectionError"):
        yahoo.YahooSource(cfg, download=download, sleep=lambda s: None).fetch()


def test_context_tickers_only_when_enabled(cfg):
    assert "^N225" not in yahoo.all_daily_tickers(cfg)
    cfg["yahoo"]["context_enabled"] = True
    assert {"^N225", "^HSI", "^KS11", "USDINR=X", "BZ=F"} <= set(yahoo.all_daily_tickers(cfg))
