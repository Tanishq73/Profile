"""Holiday calendar: weekday + CM holiday list, cached per year."""

import json
from datetime import date, timedelta

from preopen_brief.retry import SourceError
from preopen_brief.tradingday import cache_path, load_calendar

from .conftest import MORNING_NOW, load_fixture


def fetcher(counter):
    def fetch():
        counter.append(1)
        return load_fixture("morning", "holiday_master")
    return fetch


def test_holiday_weekend_and_normal_day(calendar):
    assert calendar.is_trading_day(date(2026, 11, 13))
    assert not calendar.is_trading_day(date(2026, 12, 25))
    assert calendar.why_closed(date(2026, 12, 25)) == "holiday: Christmas"
    assert calendar.why_closed(date(2026, 11, 14)) == "weekend"
    assert calendar.is_trading_day(date(2026, 2, 19))  # holiday in CD only, not CM


def test_previous_trading_day_skips_weekend_and_holiday(calendar):
    assert calendar.previous_trading_day(date(2026, 11, 16)) == date(2026, 11, 13)
    assert calendar.previous_trading_day(date(2026, 11, 11)) == date(2026, 11, 9)  # 10 Nov is a holiday
    assert calendar.trading_days_between(date(2026, 11, 9), date(2026, 11, 11)) == []
    assert calendar.trading_days_between(date(2026, 11, 12), date(2026, 11, 16)) == [date(2026, 11, 13)]


def test_cache_written_per_year_and_reused(cfg, tmp_path):
    calls = []
    first = load_calendar(MORNING_NOW, tmp_path, cfg, fetcher(calls))
    assert first.source == "nse" and calls == [1]
    blob = json.loads(cache_path(tmp_path, 2026).read_text())
    assert blob["year"] == 2026 and blob["segment"] == "CM" and len(blob["holidays"]) == 6
    second = load_calendar(MORNING_NOW, tmp_path, cfg, fetcher(calls))
    assert second.source == "cache" and calls == [1]  # no second request
    assert second.holidays == first.holidays


def test_old_cache_is_refreshed_and_survives_a_failed_refresh(cfg, tmp_path):
    calls = []
    start = MORNING_NOW.replace(month=9, day=1)          # keep all three runs inside 2026
    load_calendar(start, tmp_path, cfg, fetcher(calls))
    later = start + timedelta(days=31)
    assert load_calendar(later, tmp_path, cfg, fetcher(calls)).source == "nse"
    assert calls == [1, 1]

    def broken():
        raise SourceError("HTTP 403 after cookie refresh")

    stale = load_calendar(later + timedelta(days=31), tmp_path, cfg, broken)
    assert stale.source == "stale-cache" and not stale.is_trading_day(date(2026, 12, 25))


def test_no_list_at_all_assumes_weekdays_open(cfg, tmp_path):
    def broken():
        raise SourceError("HTTP 403")

    unknown = load_calendar(MORNING_NOW, tmp_path, cfg, broken)
    assert unknown.source == "unavailable" and unknown.holidays is None
    assert unknown.is_trading_day(date(2026, 12, 25))      # cannot know
    assert not unknown.is_trading_day(date(2026, 12, 26))  # Saturday is still closed


def test_response_for_another_year_is_not_cached(cfg, tmp_path):
    next_year = MORNING_NOW.replace(year=2027, month=1, day=4)
    result = load_calendar(next_year, tmp_path, cfg, lambda: load_fixture("morning", "holiday_master"))
    assert result.source == "unavailable"
    assert not cache_path(tmp_path, 2027).exists()
