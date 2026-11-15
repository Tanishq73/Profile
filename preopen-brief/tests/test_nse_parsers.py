"""Parsers against the saved (synthetic) fixtures, plus shape-tolerance cases."""

from datetime import date

import pytest

from preopen_brief.sources import nse
from preopen_brief.sources.nse import ParseError

from .conftest import ist, load_fixture


def test_capital_market_from_fixture():
    cm = nse.parse_capital_market(load_fixture("morning", "marketStatus"))
    assert cm["status"] == "Close"
    assert cm["trade_date"] == ist("2026-11-12T15:30:00")
    assert cm["last"] == 25875.80
    assert cm["prev_close"] == pytest.approx(25763.45)  # derived: last - variation


def test_gift_from_fixture_coerces_string_numbers():
    gift = nse.parse_gift(load_fixture("morning", "marketStatus"))
    assert gift == {
        "last": 26030.0,
        "day_change": 78.0,
        "pct_change": 0.30,
        "expiry": "2026-11-26",
        "timestamp": ist("2026-11-13T09:08:00"),
        "timestamp_raw": "13-Nov-2026 09:08",
    }


def test_gift_missing_block_names_the_field():
    raw = load_fixture("morning", "marketStatus")
    del raw["giftnifty"]
    with pytest.raises(ParseError, match="giftnifty missing"):
        nse.parse_gift(raw)
    assert nse.parse_capital_market(raw)["last"] == 25875.80  # the other half still parses


def test_gift_key_case_and_alias_tolerance():
    raw = {"giftNifty": {"lastprice": "26,030.50", "perchange": "-0.04", "expirydate": "26-Nov-2026",
                         "timestamp": "13-Nov-2026 09:08:59"}}
    gift = nse.parse_gift(raw)
    assert gift["last"] == 26030.5 and gift["pct_change"] == -0.04
    assert gift["timestamp"] == ist("2026-11-13T09:08:59")


def test_preopen_from_fixture():
    po = nse.parse_preopen(load_fixture("morning", "preopen_NIFTY"))
    assert (po["advances"], po["declines"], po["unchanged"]) == (31, 17, 2)
    assert not po["derived_counts"]
    assert len(po["stocks"]) == 50
    assert set(po["stocks"][0]) == {"symbol", "pct_change", "last_price", "prev_close"}
    assert po["timestamp"] == ist("2026-11-13T09:08:03")


def test_preopen_counts_derived_when_top_level_missing():
    raw = load_fixture("morning", "preopen_NIFTY")
    for key in ("advances", "declines", "unchanged"):
        del raw[key]
    po = nse.parse_preopen(raw)
    assert (po["advances"], po["declines"], po["unchanged"]) == (31, 17, 2)
    assert po["derived_counts"]


def test_preopen_without_counts_or_rows_fails():
    with pytest.raises(ParseError):
        nse.parse_preopen({"data": []})


def test_all_indices_from_fixture():
    parsed = nse.parse_all_indices(load_fixture("morning", "allIndices"))
    nifty = nse.get_index(parsed, "NIFTY 50")
    vix = nse.get_index(parsed, "india vix")
    assert (nifty["open"], nifty["last"], nifty["previous_close"]) == (25790.10, 25875.80, 25763.45)
    assert (vix["last"], vix["previous_close"], vix["percent_change"]) == (13.42, 13.58, -1.18)
    assert parsed["timestamp"] == ist("2026-11-12T15:30:00")


def test_all_indices_missing_vix():
    raw = load_fixture("morning", "allIndices")
    raw["data"] = [r for r in raw["data"] if r["index"] != "INDIA VIX"]
    with pytest.raises(ParseError, match="INDIA VIX not present"):
        nse.get_index(nse.parse_all_indices(raw), "INDIA VIX")


def test_holidays_cm_segment_only():
    days = nse.parse_holidays(load_fixture("morning", "holiday_master"), "CM")
    dates = [d["date"] for d in days]
    assert date(2026, 12, 25) in dates
    assert date(2026, 2, 19) not in dates  # CD-only holiday
    with pytest.raises(ParseError, match="segment XX missing"):
        nse.parse_holidays(load_fixture("morning", "holiday_master"), "XX")


@pytest.mark.parametrize("value,expected", [
    (25985.5, 25985.5), ("25,985.50", 25985.5), ("-0.04", -0.04), ("0.44%", 0.44),
    ("-", None), ("", None), (None, None), (True, None), (float("nan"), None), ("abc", None),
])
def test_to_float(value, expected):
    assert nse.to_float(value) == expected


def test_validate_reports_missing_fields():
    raw = load_fixture("morning", "marketStatus")
    assert all(ok for _, ok, _ in nse.validate("marketStatus", raw))
    del raw["giftnifty"]["TIMESTMP"]
    failed = [field for field, ok, _ in nse.validate("marketStatus", raw) if not ok]
    assert failed == ["giftnifty.timestamp"]
