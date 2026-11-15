"""NSE India: polite session client plus tolerant, pure parsers.

The endpoints are unofficial and change without notice, so:

* every field is looked up through an alias list, case-insensitively;
* a parser raises ``ParseError`` with the missing field's name rather than
  guessing;
* ``validate()`` reports, for a raw response, exactly which fields the parsers
  could and could not find (used by ``probe.py --validate``).

Field names marked SPEC came from the project brief. Names marked ASSUMED are
the author's recollection of the public response shape and were NOT verified
against a live response when this was written - run ``probe.py --validate``.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import IST
from ..retry import Deadline, SourceError, backoff_delay

log = logging.getLogger("preopen_brief.nse")

ENDPOINTS: dict[str, str] = {
    "marketStatus": "/api/marketStatus",
    "preopen_NIFTY": "/api/market-data-pre-open?key=NIFTY",
    "preopen_ALL": "/api/market-data-pre-open?key=ALL",
    "allIndices": "/api/allIndices",
    "holiday_master": "/api/holiday-master?type=trading",
}

# Logical field -> candidate keys, first match wins (case-insensitive).
ALIASES: dict[str, tuple[str, ...]] = {
    # /api/marketStatus
    "market_state": ("marketState",),                       # SPEC
    "ms_market": ("market", "marketName"),                  # ASSUMED
    "ms_status": ("marketStatus", "status"),                # ASSUMED
    "ms_trade_date": ("tradeDate", "tradedate"),            # ASSUMED
    "ms_index": ("index", "indexName"),                     # ASSUMED
    "ms_last": ("last", "lastPrice"),                       # SPEC ("last")
    "ms_variation": ("variation", "change"),                # ASSUMED
    "ms_pct": ("percentChange", "pChange"),                 # ASSUMED
    "ms_prev_close": ("previousClose", "prevClose"),        # ASSUMED (else last - variation)
    "ms_message": ("marketStatusMessage",),                 # ASSUMED
    "gift": ("giftnifty", "giftNifty"),                     # SPEC
    "gift_last": ("LASTPRICE", "lastPrice"),                # SPEC
    "gift_change": ("DAYCHANGE", "dayChange"),              # SPEC
    "gift_pct": ("PERCHANGE", "perChange"),                 # SPEC
    "gift_expiry": ("EXPIRYDATE", "expiryDate"),            # SPEC
    "gift_ts": ("TIMESTMP", "TIMESTAMP", "timestamp"),      # SPEC
    # /api/market-data-pre-open
    "po_advances": ("advances",),                           # SPEC
    "po_declines": ("declines",),                           # SPEC
    "po_unchanged": ("unchanged",),                         # SPEC
    "po_rows": ("data",),                                   # ASSUMED
    "po_metadata": ("metadata",),                           # SPEC
    "po_symbol": ("symbol",),                               # SPEC
    "po_pct": ("pChange", "perChange"),                     # SPEC
    "po_last": ("lastPrice", "iep"),                        # SPEC
    "po_prev_close": ("previousClose", "prevClose"),        # ASSUMED
    "po_timestamp": ("timestamp",),                         # ASSUMED
    # /api/allIndices
    "ai_rows": ("data",),                                   # ASSUMED
    "ai_name": ("index", "indexSymbol", "indexName"),       # ASSUMED
    "ai_open": ("open",),                                   # SPEC
    "ai_last": ("last",),                                   # SPEC
    "ai_prev_close": ("previousClose",),                    # SPEC
    "ai_pct": ("percentChange",),                           # SPEC
    "ai_variation": ("variation",),                         # ASSUMED
    "ai_timestamp": ("timestamp",),                         # ASSUMED
    # /api/holiday-master?type=trading
    "hol_date": ("tradingDate", "date"),                    # ASSUMED
    "hol_desc": ("description", "purpose"),                 # ASSUMED
}

_DATETIME_FORMATS = (
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%d-%b-%Y %I:%M %p",
    "%d-%b-%Y %I:%M:%S %p",
    "%d-%b-%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)
_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y")


class ParseError(SourceError):
    """A response did not contain a field the parser needs."""


# --------------------------------------------------------------------------- helpers


def pick(obj: Any, field: str) -> Any:
    """Value of logical ``field`` in ``obj`` via its alias list, or None."""
    if not isinstance(obj, dict):
        return None
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for alias in ALIASES[field]:
        value = lowered.get(alias.lower())
        if value is not None and value != "":
            return value
    return None


def to_float(value: Any) -> float | None:
    """Coerce NSE numbers: 25985.5, "25,985.50", "-0.04" -> float; "-", "", None -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if math.isnan(value) or math.isinf(value) else float(value)
    text = str(value).strip().replace(",", "").rstrip("%")
    if text in ("", "-", "--", "NA", "N/A", "null"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return None if number is None else int(round(number))


def parse_nse_datetime(value: Any) -> datetime | None:
    """NSE timestamps are naive strings in IST, e.g. "13-Nov-2026 09:08:01"."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse_nse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    parsed = parse_nse_datetime(text)
    return parsed.date() if parsed else None


# --------------------------------------------------------------------------- parsers


def parse_capital_market(raw: Any) -> dict[str, Any]:
    """Capital Market row of /api/marketStatus -> status, NIFTY 50 last / prev close."""
    states = pick(raw, "market_state")
    if not isinstance(states, list) or not states:
        raise ParseError("marketStatus: marketState missing")
    row = next(
        (s for s in states if "capital" in str(pick(s, "ms_market") or "").lower()),
        None,
    ) or next(
        (s for s in states if "NIFTY 50" in str(pick(s, "ms_index") or "").upper()),
        None,
    )
    if row is None:
        raise ParseError("marketStatus: no Capital Market entry")
    last = to_float(pick(row, "ms_last"))
    variation = to_float(pick(row, "ms_variation"))
    prev_close = to_float(pick(row, "ms_prev_close"))
    if prev_close is None and last is not None and variation is not None:
        prev_close = last - variation
    return {
        "status": pick(row, "ms_status"),
        "message": pick(row, "ms_message"),
        "trade_date": parse_nse_datetime(pick(row, "ms_trade_date")),
        "index": pick(row, "ms_index"),
        "last": last,
        "variation": variation,
        "percent_change": to_float(pick(row, "ms_pct")),
        "prev_close": prev_close,
    }


def parse_gift(raw: Any) -> dict[str, Any]:
    """giftnifty block of /api/marketStatus."""
    gift = pick(raw, "gift")
    if not isinstance(gift, dict):
        raise ParseError("marketStatus: giftnifty missing")
    last = to_float(pick(gift, "gift_last"))
    if last is None or last <= 0:
        raise ParseError("giftnifty: LASTPRICE missing")
    expiry_raw = pick(gift, "gift_expiry")
    expiry = parse_nse_date(expiry_raw)
    ts_raw = pick(gift, "gift_ts")
    return {
        "last": last,
        "day_change": to_float(pick(gift, "gift_change")),
        "pct_change": to_float(pick(gift, "gift_pct")),
        "expiry": expiry.isoformat() if expiry else (str(expiry_raw).strip() if expiry_raw else None),
        "timestamp": parse_nse_datetime(ts_raw),
        "timestamp_raw": ts_raw,
    }


def parse_preopen(raw: Any) -> dict[str, Any]:
    """/api/market-data-pre-open -> advances / declines / unchanged and per-stock rows."""
    if not isinstance(raw, dict):
        raise ParseError("pre-open: response is not an object")
    stocks = []
    rows = pick(raw, "po_rows")
    for item in rows if isinstance(rows, list) else []:
        meta = pick(item, "po_metadata")
        meta = meta if isinstance(meta, dict) else item
        symbol = pick(meta, "po_symbol")
        if not symbol:
            continue
        stocks.append(
            {
                "symbol": str(symbol),
                "pct_change": to_float(pick(meta, "po_pct")),
                "last_price": to_float(pick(meta, "po_last")),
                "prev_close": to_float(pick(meta, "po_prev_close")),
            }
        )
    counts = {k: to_int(pick(raw, f"po_{k}")) for k in ("advances", "declines", "unchanged")}
    derived = False
    if any(v is None for v in counts.values()):
        changes = [s["pct_change"] for s in stocks if s["pct_change"] is not None]
        if not changes:
            raise ParseError("pre-open: advances/declines missing and no per-stock pChange")
        counts = {
            "advances": sum(1 for c in changes if c > 0),
            "declines": sum(1 for c in changes if c < 0),
            "unchanged": sum(1 for c in changes if c == 0),
        }
        derived = True
    return {
        **counts,
        "derived_counts": derived,
        "timestamp": parse_nse_datetime(pick(raw, "po_timestamp")),
        "stocks": stocks,
    }


def parse_all_indices(raw: Any) -> dict[str, Any]:
    """/api/allIndices -> {"timestamp", "indices": {UPPER NAME: row}}."""
    rows = pick(raw, "ai_rows")
    if not isinstance(rows, list) or not rows:
        raise ParseError("allIndices: data missing")
    indices: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = pick(row, "ai_name")
        if not name:
            continue
        indices[" ".join(str(name).upper().split())] = {
            "open": to_float(pick(row, "ai_open")),
            "last": to_float(pick(row, "ai_last")),
            "previous_close": to_float(pick(row, "ai_prev_close")),
            "percent_change": to_float(pick(row, "ai_pct")),
            "variation": to_float(pick(row, "ai_variation")),
        }
    if not indices:
        raise ParseError("allIndices: no index rows")
    return {"timestamp": parse_nse_datetime(pick(raw, "ai_timestamp")), "indices": indices}


def get_index(parsed: dict[str, Any], name: str) -> dict[str, Any]:
    row = parsed["indices"].get(" ".join(name.upper().split()))
    if row is None:
        raise ParseError(f"allIndices: {name} not present")
    return row


def parse_holidays(raw: Any, segment: str = "CM") -> list[dict[str, Any]]:
    """/api/holiday-master?type=trading -> [{"date": date, "description": str}] for a segment."""
    if not isinstance(raw, dict):
        raise ParseError("holiday-master: response is not an object")
    rows = next((v for k, v in raw.items() if str(k).upper() == segment.upper()), None)
    if not isinstance(rows, list):
        raise ParseError(f"holiday-master: segment {segment} missing")
    holidays = []
    for row in rows:
        day = parse_nse_date(pick(row, "hol_date"))
        if day is None:
            raise ParseError("holiday-master: unparseable tradingDate")
        holidays.append({"date": day, "description": str(pick(row, "hol_desc") or "")})
    return holidays


def validate(name: str, raw: Any, holiday_segment: str = "CM") -> list[tuple[str, bool, str]]:
    """Check a raw response against what the parsers need.

    Returns ``(field, ok, detail)`` rows; used by ``probe.py --validate``.
    """
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, fn: Callable[[], Any], required: bool = True) -> Any:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - report every failure mode
            checks.append((label, False, str(exc)))
            return None
        missing = value is None
        detail = "missing" if missing else repr(value)[:60]
        checks.append((label + ("" if required else " (optional)"), not missing or not required, detail))
        return value

    if name == "marketStatus":
        cm = check("Capital Market row", lambda: parse_capital_market(raw)) or {}
        for key in ("status", "trade_date", "last", "prev_close"):
            check(f"capital_market.{key}", lambda k=key: cm.get(k))
        gift = check("giftnifty block", lambda: parse_gift(raw)) or {}
        for key in ("last", "pct_change", "expiry", "timestamp"):
            check(f"giftnifty.{key}", lambda k=key: gift.get(k))
        check("giftnifty.day_change", lambda: gift.get("day_change"), required=False)
    elif name.startswith("preopen"):
        po = check("pre-open response", lambda: parse_preopen(raw)) or {}
        for key in ("advances", "declines", "unchanged"):
            check(key, lambda k=key: po.get(k))
        check("counts present at top level", lambda: None if po.get("derived_counts") else True)
        check("per-stock rows", lambda: len(po.get("stocks") or []) or None)
        first = (po.get("stocks") or [{}])[0]
        for key in ("symbol", "pct_change", "last_price"):
            check(f"stock.{key}", lambda k=key: first.get(k))
        check("timestamp", lambda: po.get("timestamp"), required=False)
    elif name == "allIndices":
        ai = check("allIndices response", lambda: parse_all_indices(raw))
        check("timestamp", lambda: (ai or {}).get("timestamp"), required=False)
        for index, keys in (
            ("NIFTY 50", ("open", "last", "previous_close")),
            ("INDIA VIX", ("last", "previous_close", "percent_change")),
        ):
            row = check(index, lambda i=index: get_index(ai, i) if ai else None) or {}
            for key in keys:
                check(f"{index}.{key}", lambda k=key, r=row: r.get(k))
    elif name == "holiday_master":
        days = check(f"{holiday_segment} holidays", lambda: parse_holidays(raw, holiday_segment))
        check("holiday count", lambda: len(days or []) or None)
    else:
        checks.append((name, False, "unknown endpoint"))
    return checks


# --------------------------------------------------------------------------- client


class _TransportFailure(Exception):
    pass


class _HttpxBackend:
    def __init__(self, connect: float, read: float, transport: httpx.BaseTransport | None):
        self._connect, self._read = connect, read
        self._client = httpx.Client(follow_redirects=True, transport=transport)

    def get(self, url: str, headers: dict[str, str], cap: float) -> tuple[int, str]:
        timeout = httpx.Timeout(min(self._read, cap), connect=min(self._connect, cap))
        try:
            response = self._client.get(url, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise _TransportFailure(type(exc).__name__) from exc
        return response.status_code, response.text

    def close(self) -> None:
        self._client.close()


class _CurlCffiBackend:
    """Experimental: browser TLS fingerprint via curl_cffi (already installed with yfinance)."""

    def __init__(self, connect: float, read: float):
        from curl_cffi import requests as curl_requests

        self._connect, self._read = connect, read
        self._session = curl_requests.Session(impersonate="chrome")

    def get(self, url: str, headers: dict[str, str], cap: float) -> tuple[int, str]:
        keep = {k: v for k, v in headers.items() if k in ("Referer", "Accept", "X-Requested-With")}
        try:
            response = self._session.get(
                url, headers=keep, timeout=(min(self._connect, cap), min(self._read, cap))
            )
        except Exception as exc:  # noqa: BLE001 - curl_cffi raises its own hierarchy
            raise _TransportFailure(type(exc).__name__) from exc
        return response.status_code, response.text

    def close(self) -> None:
        self._session.close()


class NseClient:
    """One instance per run: shared cookies, request cap, spacing, one cookie refresh."""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        transport: httpx.BaseTransport | None = None,
        deadline: Deadline | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ):
        nse, http = cfg["nse"], cfg["http"]
        self._base = nse["base_url"].rstrip("/")
        self._ua = nse["user_agent"]
        self._max_requests = int(nse["max_requests_per_run"])
        self._min_interval = max(0.4, float(nse["min_interval_ms"]) / 1000.0)
        self._max_retries = int(nse["max_retries"])
        self._backoff = (float(http["backoff_base"]), float(http["backoff_max"]))
        self._deadline = deadline
        self._sleep, self._monotonic, self._rng = sleep, monotonic, rng
        if nse.get("backend") == "curl_cffi" and transport is None:
            self._http: Any = _CurlCffiBackend(http["connect_timeout"], http["read_timeout"])
        else:
            self._http = _HttpxBackend(http["connect_timeout"], http["read_timeout"], transport)
        self.requests_made = 0
        self._last_request: float | None = None
        self._bootstrapped = False
        self._refreshed = False
        self._dead: str | None = None
        self._last_error: str | None = None

    def close(self) -> None:
        self._http.close()

    # -- low level ---------------------------------------------------------

    def _headers(self, api: bool) -> dict[str, str]:
        common = {
            "User-Agent": self._ua,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if api:
            return {
                **common,
                "Accept": "application/json, text/plain, */*",
                "Referer": self._base + "/",
                "X-Requested-With": "XMLHttpRequest",
            }
        return {
            **common,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }

    def _request(self, url: str, *, api: bool) -> tuple[int, str]:
        if self.requests_made >= self._max_requests:
            why = f" after {self._last_error}" if self._last_error else ""
            raise SourceError(f"NSE request cap reached ({self._max_requests}){why}")
        cap = self._deadline.remaining() if self._deadline else 3600.0
        if cap <= 0.5:
            raise SourceError("run budget exhausted")
        if self._last_request is not None:
            wait = self._min_interval - (self._monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._monotonic()
        self.requests_made += 1
        return self._http.get(url, self._headers(api), cap)

    def _bootstrap(self) -> None:
        """GET the homepage so the session picks up NSE's cookies."""
        self._bootstrapped = True
        try:
            status, _ = self._request(self._base + "/", api=False)
        except _TransportFailure as exc:
            log.warning("nse homepage failed: %s", exc)
            return
        if status != 200:
            log.warning("nse homepage returned HTTP %s", status)

    # -- public ------------------------------------------------------------

    def fetch(self, name: str) -> Any:
        """Fetch one endpoint by name and return its decoded JSON.

        401/403 (or a non-JSON body, NSE's soft block) triggers exactly one
        cookie refresh per run; if it still fails NSE is marked failed and
        later calls fail fast without spending requests.
        """
        if self._dead:
            raise SourceError(self._dead)
        if not self._bootstrapped:
            self._bootstrap()
        url = self._base + ENDPOINTS[name]
        attempt = 0
        while True:
            attempt += 1
            blocked = None
            try:
                status, body = self._request(url, api=True)
            except _TransportFailure as exc:
                error = f"network error ({exc})"
            else:
                if status in (401, 403):
                    blocked = f"HTTP {status}"
                elif status == 200:
                    try:
                        return json.loads(body)
                    except ValueError:
                        blocked = "non-JSON response"
                elif status == 429 or status >= 500:
                    error = f"HTTP {status}"
                else:
                    raise SourceError(f"HTTP {status}")
            if blocked:
                if self._refreshed:
                    self._dead = f"{blocked} after cookie refresh"
                    raise SourceError(self._dead)
                self._refreshed = True
                log.info("nse %s: %s, refreshing cookies once", name, blocked)
                self._bootstrap()
                attempt -= 1
                continue
            self._last_error = error
            if attempt > self._max_retries:
                raise SourceError(error)
            self._sleep(backoff_delay(attempt, *self._backoff, rng=self._rng))


class FixtureNse:
    """Offline stand-in for NseClient: serves the newest ``<endpoint>_*.json`` in a directory."""

    def __init__(self, directory: Path | str):
        self._dir = Path(directory)
        self.requests_made = 0

    def fetch(self, name: str) -> Any:
        matches = sorted(self._dir.glob(f"{name}_*.json"))
        if not matches:
            raise SourceError(f"no fixture for {name}")
        with open(matches[-1], encoding="utf-8") as fh:
            return json.load(fh)

    def close(self) -> None:
        pass
