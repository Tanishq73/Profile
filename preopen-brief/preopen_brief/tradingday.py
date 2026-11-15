"""Trading-day logic: weekday + NSE trading-holiday list (CM segment), cached per year."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .retry import SourceError
from .sources import nse

log = logging.getLogger("preopen_brief.tradingday")


@dataclass
class Calendar:
    """Holiday knowledge for one run. ``holidays is None`` means the list is unknown."""

    holidays: dict[date, str] | None
    source: str  # "cache" | "nse" | "stale-cache" | "unavailable"

    def is_trading_day(self, day: date) -> bool:
        """Mon-Fri and not a listed holiday. With no list, weekdays are assumed open."""
        if day.weekday() >= 5:
            return False
        return not (self.holidays and day in self.holidays)

    def why_closed(self, day: date) -> str | None:
        if day.weekday() >= 5:
            return "weekend"
        if self.holidays and day in self.holidays:
            return f"holiday: {self.holidays[day]}".rstrip(": ")
        return None

    def previous_trading_day(self, day: date) -> date:
        cursor = day - timedelta(days=1)
        while not self.is_trading_day(cursor):
            cursor -= timedelta(days=1)
        return cursor

    def trading_days_between(self, start: date, end: date) -> list[date]:
        """Trading days strictly between ``start`` and ``end``."""
        days, cursor = [], start + timedelta(days=1)
        while cursor < end:
            if self.is_trading_day(cursor):
                days.append(cursor)
            cursor += timedelta(days=1)
        return days


def cache_path(data_dir: Path, year: int) -> Path:
    return data_dir / f"holidays_{year}.json"


def _read_cache(path: Path) -> tuple[dict[date, str], datetime] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        holidays = {date.fromisoformat(h["date"]): h.get("description", "") for h in blob["holidays"]}
        return holidays, datetime.fromisoformat(blob["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_calendar(
    today: datetime,
    data_dir: Path,
    cfg: dict[str, Any],
    fetch_raw: Callable[[], Any] | None,
) -> Calendar:
    """Holiday list for ``today``'s year from data/holidays_<year>.json, fetching when
    the cache is missing or older than ``holiday_cache_max_age_days``.

    ``fetch_raw`` returns the raw holiday-master JSON (one NSE request) or raises
    SourceError; pass None to forbid network use.
    """
    calendar = _load_year(today, data_dir, cfg, fetch_raw)
    # Looking back across New Year needs last year's list too; use it if cached.
    previous = _read_cache(cache_path(data_dir, today.year - 1))
    if previous and calendar.holidays is not None:
        calendar.holidays = {**previous[0], **calendar.holidays}
    return calendar


def _load_year(
    today: datetime,
    data_dir: Path,
    cfg: dict[str, Any],
    fetch_raw: Callable[[], Any] | None,
) -> Calendar:
    year = today.year
    path = cache_path(data_dir, year)
    cached = _read_cache(path)
    max_age = timedelta(days=int(cfg["nse"]["holiday_cache_max_age_days"]))
    if cached and today - cached[1] <= max_age:
        return Calendar(cached[0], "cache")
    if fetch_raw is not None:
        try:
            rows = nse.parse_holidays(fetch_raw(), cfg["nse"]["holiday_segment"])
            holidays = {h["date"]: h["description"] for h in rows if h["date"].year == year}
            if not holidays:
                raise SourceError(f"holiday-master: no {year} dates in response")
            data_dir.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "year": year,
                        "segment": cfg["nse"]["holiday_segment"],
                        "fetched_at": today.isoformat(),
                        "holidays": [
                            {"date": d.isoformat(), "description": desc} for d, desc in sorted(holidays.items())
                        ],
                    },
                    fh,
                    indent=1,
                )
            return Calendar(holidays, "nse")
        except SourceError as exc:
            log.warning("holiday list fetch failed: %s", exc)
    if cached:
        return Calendar(cached[0], "stale-cache")
    return Calendar(None, "unavailable")
