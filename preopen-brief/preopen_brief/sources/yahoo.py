"""Yahoo Finance via yfinance: batched fetch plus pure helpers over a plain payload.

The fetch converts yfinance DataFrames into a JSON-friendly payload once::

    {"daily":    {ticker: [{"t": "YYYY-MM-DD", "o":, "h":, "l":, "c":}, ...]},
     "intraday": {ticker: [{"t": "<UTC ISO bar start>", "c":}, ...]},
     "errors":   {"daily": "...", "intraday": "..."}}

Everything downstream (and every test) works on that payload, never on the
network. yfinance is unofficial and rate-limited; all failures become reasons.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import NEW_YORK
from ..retry import SourceError, backoff_delay

log = logging.getLogger("preopen_brief.yahoo")

US_CASH_CLOSE = dtime(16, 0)  # New York wall-clock; zoneinfo handles DST


# --------------------------------------------------------------------------- fetch


def all_daily_tickers(cfg: dict[str, Any]) -> list[str]:
    y = cfg["yahoo"]
    tickers = list(y["us_cash"]) + list(y["us_futures"]) + [y["india_vix"], y["nifty"]]
    if y.get("context_enabled"):
        tickers += list(y["context"])
    return tickers


def _clean(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def frame_to_bars(frame: Any, tickers: list[str], *, intraday: bool) -> dict[str, list[dict[str, Any]]]:
    """Convert a ``yf.download(..., group_by="ticker")`` frame to payload bars.

    A ticker that Yahoo did not return (absent column, or all-NaN closes) maps
    to an empty list rather than raising.
    """
    import pandas as pd

    out: dict[str, list[dict[str, Any]]] = {t: [] for t in tickers}
    if frame is None or len(frame) == 0:
        return out
    multi = isinstance(frame.columns, pd.MultiIndex)
    top = set(frame.columns.get_level_values(0)) if multi else set()
    for ticker in tickers:
        if multi:
            if ticker not in top:
                continue
            sub = frame[ticker]
        elif len(tickers) == 1:
            sub = frame
        else:
            continue
        if "Close" not in sub.columns:
            continue
        sub = sub[sub["Close"].notna()]
        bars = []
        for stamp, row in sub.iterrows():
            close = _clean(row["Close"])
            if close is None:
                continue
            if intraday:
                ts = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
                bars.append({"t": ts.isoformat(), "c": close})
            else:
                bars.append(
                    {
                        "t": stamp.date().isoformat(),
                        "o": _clean(row.get("Open")),
                        "h": _clean(row.get("High")),
                        "l": _clean(row.get("Low")),
                        "c": close,
                    }
                )
        out[ticker] = bars
    return out


class YahooSource:
    """Batched yfinance downloads with retry + backoff. One instance per run."""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        download: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ):
        self._cfg = cfg
        self._download = download
        self._sleep, self._rng = sleep, rng

    def _call(self, tickers: list[str], period: str, interval: str) -> Any:
        download = self._download
        if download is None:
            import yfinance as yf

            download = yf.download
        y, http = self._cfg["yahoo"], self._cfg["http"]
        last_error = "no data"
        for attempt in range(1, int(y["max_retries"]) + 2):
            try:
                frame = download(
                    tickers=tickers,
                    period=period,
                    interval=interval,
                    group_by="ticker",
                    auto_adjust=False,
                    progress=False,
                    threads=True,
                    timeout=int(y["call_timeout_seconds"]),
                )
                if frame is not None and len(frame) > 0:
                    return frame
                last_error = "empty response"
            except Exception as exc:  # noqa: BLE001 - yfinance raises many types
                last_error = f"{type(exc).__name__}"
                log.warning("yfinance %s/%s attempt %d failed: %s", period, interval, attempt, exc)
            if attempt <= int(y["max_retries"]):
                self._sleep(backoff_delay(attempt, float(http["backoff_base"]), float(http["backoff_max"]), self._rng))
        raise SourceError(f"yfinance: {last_error}")

    def fetch(self, *, daily_tickers: list[str] | None = None, with_intraday: bool | None = None) -> dict[str, Any]:
        """Daily history for every ticker (one batched call) and, when
        ``futures_reference: cash_close``, hourly bars for the index futures (a second one)."""
        y = self._cfg["yahoo"]
        daily_tickers = daily_tickers or all_daily_tickers(self._cfg)
        if with_intraday is None:
            with_intraday = y["futures_reference"] == "cash_close"
        payload: dict[str, Any] = {"daily": {}, "intraday": {}, "errors": {}}
        try:
            frame = self._call(daily_tickers, y["history_period"], "1d")
            payload["daily"] = frame_to_bars(frame, daily_tickers, intraday=False)
        except SourceError as exc:
            payload["errors"]["daily"] = str(exc)
        if with_intraday and "daily" in payload["errors"]:
            payload["errors"]["intraday"] = "skipped: daily call failed"
        elif with_intraday:
            futures = list(y["us_futures"])
            try:
                frame = self._call(futures, y["futures_intraday_period"], y["futures_intraday_interval"])
                payload["intraday"] = frame_to_bars(frame, futures, intraday=True)
            except SourceError as exc:
                payload["errors"]["intraday"] = str(exc)
        if "daily" in payload["errors"] and not any(payload["intraday"].values()):
            raise SourceError(payload["errors"]["daily"])
        return payload


class FixtureYahoo:
    """Offline stand-in: serves the newest ``yahoo_*.json`` payload in a directory."""

    def __init__(self, directory: Path | str):
        self._dir = Path(directory)

    def fetch(self, **_: Any) -> dict[str, Any]:
        matches = sorted(self._dir.glob("yahoo_*.json"))
        if not matches:
            raise SourceError("no fixture for yahoo")
        with open(matches[-1], encoding="utf-8") as fh:
            return json.load(fh)


# --------------------------------------------------------------------------- pure helpers


def bars_for(payload: dict[str, Any] | None, ticker: str, kind: str = "daily") -> list[dict[str, Any]]:
    if not payload:
        return []
    return list((payload.get(kind) or {}).get(ticker) or [])


def daily_change(bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """% change of the latest daily bar vs the one before it, or None if < 2 bars."""
    if len(bars) < 2 or not bars[-2]["c"]:
        return None
    last, prev = bars[-1], bars[-2]
    return {
        "pct": (last["c"] / prev["c"] - 1.0) * 100.0,
        "last": last["c"],
        "prev": prev["c"],
        "date": date.fromisoformat(last["t"]),
    }


def closes(bars: list[dict[str, Any]], window: int, before: date | None = None) -> list[float]:
    """Trailing ``window`` daily closes, optionally only sessions strictly before ``before``."""
    values = [b["c"] for b in bars if before is None or date.fromisoformat(b["t"]) < before]
    return values[-window:]


def us_cash_close_reference(now: datetime) -> datetime:
    """Most recent weekday 16:00 New York that is not after ``now`` (UTC-aware result).

    Wall-clock arithmetic in America/New_York keeps this right across US DST
    changes: 16:00 ET is 20:00 UTC in summer and 21:00 UTC in winter, while
    India never shifts. US exchange holidays are not modelled; index futures
    still trade and settle on most of them.
    """
    local = now.astimezone(NEW_YORK)
    day = local.date()
    if local.time() < US_CASH_CLOSE:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, US_CASH_CLOSE, tzinfo=NEW_YORK).astimezone(timezone.utc)


def us_session_in_progress(session_date: date, now: datetime) -> bool:
    """True if ``session_date`` is today's still-open US cash session."""
    local = now.astimezone(NEW_YORK)
    return session_date == local.date() and local.time() < US_CASH_CLOSE


def futures_change(
    intraday_bars: list[dict[str, Any]],
    now: datetime,
    max_age_minutes: float,
) -> dict[str, Any]:
    """Live futures % change vs the price at the prior US cash close (~ settlement).

    Reference = close of the last bar that started before 16:00 New York on the
    most recent completed US session. Raises SourceError when there is no
    reference bar, no trade since, or the latest bar is too old to call live.
    """
    if not intraday_bars:
        raise SourceError("no intraday bars")
    ref_time = us_cash_close_reference(now)
    parsed = [(datetime.fromisoformat(b["t"]), b["c"]) for b in intraday_bars]
    before = [(t, c) for t, c in parsed if t < ref_time]
    after = [(t, c) for t, c in parsed if t >= ref_time]
    if not before:
        raise SourceError("no bar before US cash close")
    if not after:
        raise SourceError("no trade since US cash close")
    ref_t, ref_c = before[-1]
    if ref_time - ref_t > timedelta(hours=6):
        raise SourceError("reference bar too far from US cash close")
    last_t, last_c = after[-1]
    age = (now.astimezone(timezone.utc) - last_t).total_seconds() / 60.0
    if age > max_age_minutes:
        raise SourceError(f"stale quote ({int(age)} min old)")
    return {
        "pct": (last_c / ref_c - 1.0) * 100.0,
        "last": last_c,
        "reference": ref_c,
        "reference_time": ref_time.isoformat(),
        "last_time": last_t.isoformat(),
    }
