"""Regenerate tests/fixtures/. Run: python tests/make_synthetic_fixtures.py

EVERYTHING THIS WRITES IS SYNTHETIC. No value came from NSE or Yahoo. The NSE
shapes use the field names from the project brief plus the author's unverified
recollection of the public responses; ``probe.py --validate`` on a real
response is the source of truth.

Scenario (one coherent story across three directories):
  close_prev/  Thu 12 Nov 2026 15:32 IST  - the previous session's close snapshot
  morning/     Fri 13 Nov 2026 09:09 IST  - pre-open cues
  close/       Fri 13 Nov 2026 15:32 IST  - that day's outcome
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent / "fixtures"
NOTE = "SYNTHETIC test fixture - hand-made, NOT a real NSE/Yahoo response"


def write(directory: str, name: str, data: dict) -> None:
    path = ROOT / directory / f"{name}_SYNTHETIC.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"_synthetic": NOTE, **data}, fh, indent=1)
        fh.write("\n")


def market_status(trade_date: str, status: str, last: float, variation: float, pct: float,
                  gift_last: float, gift_change: str, gift_pct: str, gift_ts: str) -> dict:
    return {
        "marketState": [
            {"market": "Capital Market", "marketStatus": status, "tradeDate": trade_date, "index": "NIFTY 50",
             "last": last, "variation": variation, "percentChange": pct,
             "marketStatusMessage": "Market is Closed" if status == "Close" else "Normal Market is Open"},
            {"market": "Currency", "marketStatus": "Close", "tradeDate": trade_date, "index": "",
             "last": "", "variation": "", "percentChange": "", "marketStatusMessage": "Market is Closed"},
            {"market": "Commodity", "marketStatus": "Close", "tradeDate": trade_date, "index": "",
             "last": "", "variation": "", "percentChange": "", "marketStatusMessage": "Market is Closed"},
        ],
        "giftnifty": {
            "INSTRUMENTTYPE": "FUTIDX", "SYMBOL": "NIFTY", "EXPIRYDATE": "26-Nov-2026", "OPTIONTYPE": "XX",
            "STRIKEPRICE": "-", "LASTPRICE": gift_last, "DAYCHANGE": gift_change, "PERCHANGE": gift_pct,
            "CONTRACTSTRADED": 41873, "TIMESTMP": gift_ts,
        },
    }


def all_indices(timestamp: str, nifty: tuple[float, float, float], vix: tuple[float, float]) -> dict:
    n_open, n_last, n_prev = nifty
    v_last, v_prev = vix

    def row(name: str, open_: float, last: float, prev: float) -> dict:
        return {"key": "BROAD MARKET INDICES", "index": name, "indexSymbol": name, "last": last,
                "variation": round(last - prev, 2), "percentChange": round((last / prev - 1) * 100, 2),
                "open": open_, "high": max(open_, last), "low": min(open_, last), "previousClose": prev}

    return {
        "data": [
            row("NIFTY 50", n_open, n_last, n_prev),
            row("NIFTY NEXT 50", 70110.0, 70402.55, 70098.2),
            row("NIFTY BANK", 58020.4, 58311.9, 58102.75),
            row("INDIA VIX", v_prev, v_last, v_prev),
        ],
        "timestamp": timestamp,
    }


def preopen(key: str, advances: int, declines: int, unchanged: int, rows: int, timestamp: str) -> dict:
    rng = random.Random(f"preopen-{key}")
    signs = [1] * advances + [-1] * declines + [0] * unchanged
    data = []
    for i in range(rows):
        sign = signs[i % len(signs)] if rows < len(signs) else signs[i]
        prev = round(rng.uniform(150, 4200), 2)
        pct = round(sign * rng.uniform(0.05, 1.8), 2)
        last = round(prev * (1 + pct / 100), 2)
        data.append({
            "metadata": {"symbol": f"SYN{key[:1]}{i + 1:03d}", "identifier": f"SYN{key[:1]}{i + 1:03d}EQN",
                         "lastPrice": last, "change": round(last - prev, 2), "pChange": pct,
                         "previousClose": prev, "finalQuantity": rng.randint(100, 90000), "iep": last},
            "detail": {"preOpenMarket": {"IEP": last, "lastUpdateTime": timestamp,
                                         "totalBuyQuantity": rng.randint(1000, 500000),
                                         "totalSellQuantity": rng.randint(1000, 500000)}},
        })
    return {"declines": declines, "unchanged": unchanged, "data": data, "advances": advances, "timestamp": timestamp}


HOLIDAYS = {
    "CM": [
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day", "Sr_no": 1},
        {"tradingDate": "03-Apr-2026", "weekDay": "Friday", "description": "Good Friday", "Sr_no": 2},
        {"tradingDate": "01-May-2026", "weekDay": "Friday", "description": "Maharashtra Day", "Sr_no": 3},
        {"tradingDate": "02-Oct-2026", "weekDay": "Friday", "description": "Mahatma Gandhi Jayanti", "Sr_no": 4},
        {"tradingDate": "10-Nov-2026", "weekDay": "Tuesday", "description": "Diwali Balipratipada", "Sr_no": 5},
        {"tradingDate": "25-Dec-2026", "weekDay": "Friday", "description": "Christmas", "Sr_no": 6},
    ],
    "FO": [{"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day", "Sr_no": 1}],
    "CD": [{"tradingDate": "19-Feb-2026", "weekDay": "Thursday", "description": "Synthetic CD-only holiday", "Sr_no": 1}],
}


def daily_series(ticker: str, last_day: date, final_closes: list[float], sessions: int = 252) -> list[dict]:
    """Weekday bars ending on ``last_day``; a seeded random walk whose tail is ``final_closes``."""
    rng = random.Random(f"daily-{ticker}")
    days = []
    cursor = last_day
    while len(days) < sessions:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    walk = [final_closes[0]]
    for _ in range(sessions - len(final_closes)):
        walk.append(walk[-1] / (1 + rng.gauss(0.0003, 0.011 if ticker != "^INDIAVIX" else 0.035)))
    if ticker == "^INDIAVIX":
        walk = [min(max(v, 10.6), 23.5) for v in walk]
    closes = list(reversed(walk[1:])) + final_closes
    bars = []
    for day, close in zip(days, closes):
        open_ = close * (1 + rng.uniform(-0.003, 0.003))
        bars.append({"t": day.isoformat(), "o": round(open_, 2), "h": round(max(open_, close) * 1.002, 2),
                     "l": round(min(open_, close) * 0.998, 2), "c": round(close, 2)})
    return bars


def intraday_series(reference: float, last: float, ref_start: datetime, last_start: datetime) -> list[dict]:
    """Hourly bars (UTC bar starts): flat-ish into ``ref_start``, then drifting to ``last``."""
    bars = []
    cursor = ref_start - timedelta(hours=30)
    while cursor <= ref_start:
        drift = reference * (1 - 0.0004 * ((ref_start - cursor).total_seconds() / 3600) ** 0.5)
        bars.append({"t": cursor.isoformat(), "c": round(drift if cursor < ref_start else reference, 2)})
        cursor += timedelta(hours=1)
    # After the cash close; 22:00 UTC (17:00 EST) is CME's daily maintenance hour, so no bar there.
    cursor = ref_start + timedelta(hours=1)
    steps = int((last_start - cursor).total_seconds() // 3600)
    for i in range(steps + 1):
        if cursor.hour != 22:
            bars.append({"t": cursor.isoformat(), "c": round(reference + (last - reference) * (i / max(steps, 1)), 2)})
        cursor += timedelta(hours=1)
    return bars


def with_change(prev: float, pct: float) -> list[float]:
    return [prev, round(prev * (1 + pct / 100), 2)]


def yahoo_morning() -> dict:
    us = date(2026, 11, 12)   # last completed US session (Thursday, EST)
    india = date(2026, 11, 12)
    daily = {
        "^GSPC": daily_series("^GSPC", us, with_change(7312.40, 0.42)),
        "^IXIC": daily_series("^IXIC", us, with_change(24105.10, 0.61)),
        "^DJI": daily_series("^DJI", us, with_change(49876.00, 0.18)),
        "ES=F": daily_series("ES=F", us, with_change(7340.00, 0.40)),
        "NQ=F": daily_series("NQ=F", us, with_change(26480.00, 0.55)),
        "YM=F": daily_series("YM=F", us, with_change(50010.00, 0.20)),
        "^INDIAVIX": daily_series("^INDIAVIX", india, [13.58, 13.42]),
        "^NSEI": daily_series("^NSEI", india, [25763.45, 25875.80]),
    }
    ref_start = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)   # 15:00 EST bar -> closes at the cash close
    last_start = datetime(2026, 11, 13, 3, 0, tzinfo=timezone.utc)   # 08:30 IST bar, live at 09:09 IST
    intraday = {
        "ES=F": intraday_series(7369.50, round(7369.50 * 1.0012, 2), ref_start, last_start),
        "NQ=F": intraday_series(26625.75, round(26625.75 * 1.0020, 2), ref_start, last_start),
        "YM=F": intraday_series(50110.00, round(50110.00 * 1.0005, 2), ref_start, last_start),
    }
    return {"daily": daily, "intraday": intraday, "errors": {}}


def yahoo_close() -> dict:
    bars = daily_series("^NSEI", date(2026, 11, 12), [25763.45, 25875.80])
    bars.append({"t": "2026-11-13", "o": 25951.2, "h": 25990.0, "l": 25902.3, "c": 25968.4})
    return {"daily": {"^NSEI": bars}, "intraday": {}, "errors": {}}


def main() -> None:
    # Thursday close: the snapshot Friday's basis-adjusted gap is measured from.
    write("close_prev", "marketStatus", market_status(
        "12-Nov-2026 15:30", "Close", 25875.80, 112.35, 0.44, 25948.0, "121.00", "0.47", "12-Nov-2026 15:31"))
    write("close_prev", "allIndices", all_indices("12-Nov-2026 15:30", (25790.10, 25875.80, 25763.45), (13.42, 13.58)))
    write("close_prev", "holiday_master", HOLIDAYS)

    # Friday 09:09: NSE still serves Thursday's session for indices; GIFT and the pre-open book are live.
    write("morning", "marketStatus", market_status(
        "12-Nov-2026 15:30", "Close", 25875.80, 112.35, 0.44, 26030.0, "78.00", "0.30", "13-Nov-2026 09:08"))
    write("morning", "allIndices", all_indices("12-Nov-2026 15:30", (25790.10, 25875.80, 25763.45), (13.42, 13.58)))
    write("morning", "preopen_NIFTY", preopen("NIFTY", 31, 17, 2, 50, "13-Nov-2026 09:08:03"))
    write("morning", "preopen_ALL", preopen("ALL", 1040, 890, 95, 12, "13-Nov-2026 09:08:03"))
    write("morning", "holiday_master", HOLIDAYS)
    write("morning", "yahoo", yahoo_morning())

    # Friday 15:32: outcome.
    write("close", "marketStatus", market_status(
        "13-Nov-2026 15:30", "Close", 25968.40, 92.60, 0.36, 26041.0, "89.00", "0.34", "13-Nov-2026 15:31"))
    write("close", "allIndices", all_indices("13-Nov-2026 15:30", (25951.20, 25968.40, 25875.80), (13.05, 13.42)))
    write("close", "holiday_master", HOLIDAYS)
    write("close", "yahoo", yahoo_close())


if __name__ == "__main__":
    main()
