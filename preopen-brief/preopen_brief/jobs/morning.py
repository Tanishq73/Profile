"""09:09 IST morning job: collect cues, compute signals, send the brief.

    python -m preopen_brief.jobs.morning [--dry-run] [--offline DIR --now ISO]
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .. import llm, notify
from ..composite import Composite, composite_bias
from ..retry import SourceError, SourceResult
from ..signals import (
    Signal,
    breadth_signal,
    failed,
    gift_gap_signal,
    mean_change_signal,
    resolve_nifty_prev_close,
    vix_signal,
)
from ..sources import nse, yahoo
from ..tradingday import Calendar
from .common import Background, RunContext, deliver, fetch_source, finish, run_job, skip_if_closed, trading_calendar

NSE_SOURCES = ("marketStatus", "preopen_NIFTY", "preopen_ALL", "allIndices")


@dataclass
class MorningResult:
    signals: dict[str, Signal]
    composite: Composite
    context: dict[str, float | None] | None
    cross_checks: dict[str, Any]


def _parse(result: SourceResult, parser, label: str) -> tuple[Any, str | None]:
    """Run a parser over a source result -> (parsed | None, reason | None)."""
    if not result.ok:
        return None, f"{label}: {result.error}"
    try:
        return parser(result.data), None
    except SourceError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - a surprising shape must not sink the run
        return None, f"{label}: unexpected shape ({type(exc).__name__})"


def _us_cash(payload: dict | None, yahoo_error: str | None, today: date, now: datetime,
             calendar: Calendar, cfg: dict[str, Any]) -> Signal:
    changes: dict[str, float | None] = {}
    reasons: dict[str, str] = {}
    sessions: list[date] = []
    for ticker, display in cfg["yahoo"]["us_cash"].items():
        change = yahoo.daily_change(yahoo.bars_for(payload, ticker))
        if change is None:
            changes[display] = None
            reasons[display] = yahoo_error or f"yahoo: no data for {ticker}"
        else:
            changes[display] = change["pct"]
            sessions.append(change["date"])
    signal = mean_change_signal("us_cash", changes, cfg["signals"]["us_cash"], reasons)
    if not signal.ok or not sessions:
        return signal
    session = max(sessions)
    signal.detail["session_date"] = session.isoformat()
    # A US session that an Indian session has already traded on is old news.
    if calendar.trading_days_between(session, today):
        return failed("us_cash", f"stale: last US session {session:%d %b}", **signal.detail)
    if yahoo.us_session_in_progress(session, now):
        signal.flags.append("US session in progress")
    return signal


def _us_futures(payload: dict | None, yahoo_error: str | None, now: datetime, cfg: dict[str, Any]) -> Signal:
    ycfg = cfg["yahoo"]
    intraday_error = ((payload or {}).get("errors") or {}).get("intraday")
    use_intraday = ycfg["futures_reference"] == "cash_close" and payload is not None and not intraday_error
    changes: dict[str, float | None] = {}
    reasons: dict[str, str] = {}
    for ticker, display in ycfg["us_futures"].items():
        try:
            if use_intraday:
                change = yahoo.futures_change(
                    yahoo.bars_for(payload, ticker, "intraday"), now, float(ycfg["futures_max_age_minutes"])
                )
            else:
                change = yahoo.daily_change(yahoo.bars_for(payload, ticker))
                if change is None:
                    raise SourceError(yahoo_error or f"yahoo: no data for {ticker}")
            changes[display] = change["pct"]
        except SourceError as exc:
            changes[display] = None
            reasons[display] = str(exc) if str(exc).startswith("yahoo") else f"yahoo {ticker}: {exc}"
    signal = mean_change_signal("us_futures", changes, cfg["signals"]["us_futures"], reasons)
    signal.detail["reference"] = "US cash close (16:00 New York)" if use_intraday else "previous daily bar"
    if signal.ok and not use_intraday:
        signal.flags.append("daily-bar ref")
    return signal


def _vix(ai: dict | None, ai_error: str | None, payload: dict | None, fetched: datetime, cfg: dict[str, Any]) -> Signal:
    vcfg = cfg["signals"]["vix"]
    bars = yahoo.bars_for(payload, cfg["yahoo"]["india_vix"])
    history = yahoo.closes(bars, int(vcfg["percentile_window"]))
    level = change = None
    error = ai_error
    if ai is not None:
        try:
            row = nse.get_index(ai, "INDIA VIX")
            level, change = row["last"], row["percent_change"]
            if change is None and row["last"] and row["previous_close"]:
                change = (row["last"] / row["previous_close"] - 1.0) * 100.0
        except SourceError as exc:
            error = str(exc)
    if level:
        return vix_signal(level, change, history, fetched.time(), vcfg)
    fallback = yahoo.daily_change(bars)
    if fallback is not None:
        signal = vix_signal(fallback["last"], fallback["pct"], history, fetched.time(), vcfg, source="yahoo")
        signal.detail["nse_error"] = error
        return signal
    return vix_signal(None, None, history, fetched.time(), vcfg, error=error)


def compute_morning(
    now: datetime,
    cfg: dict[str, Any],
    nse_results: dict[str, SourceResult],
    yahoo_result: SourceResult,
    snapshot: dict[str, Any] | None,
    calendar: Calendar,
    scored_days: int,
) -> MorningResult:
    """All signals from already-fetched sources. No I/O, so it is fully testable offline."""
    today = now.date()
    payload = yahoo_result.data if yahoo_result.ok else None
    yahoo_error = None if yahoo_result.ok else f"yahoo: {yahoo_result.error}"
    signals: dict[str, Signal] = {}

    signals["us_cash"] = _us_cash(payload, yahoo_error, today, now, calendar, cfg)
    signals["us_futures"] = _us_futures(payload, yahoo_error, now, cfg)

    ms = nse_results["marketStatus"]
    capital_market, _ = _parse(ms, nse.parse_capital_market, "NSE marketStatus")
    gift, gift_error = _parse(ms, nse.parse_gift, "NSE marketStatus")
    ai_result = nse_results["allIndices"]
    ai, ai_error = _parse(ai_result, nse.parse_all_indices, "NSE allIndices")

    snapshot_problem = None
    if snapshot is not None:
        snap_date = date.fromisoformat(snapshot["trade_date"])
        if calendar.trading_days_between(snap_date, today):
            snapshot_problem = f"no snapshot for previous session (latest {snap_date:%d %b})"
    nifty_index = ai["indices"].get("NIFTY 50") if ai else None
    if snapshot is not None and snapshot_problem is None and snapshot.get("nifty_close"):
        prev_close, prev_source = snapshot["nifty_close"], "close snapshot"
    else:
        prev_close, prev_source = resolve_nifty_prev_close(
            today, capital_market, nifty_index, ai["timestamp"] if ai else None,
            yahoo.bars_for(payload, cfg["yahoo"]["nifty"]),
        )
    signals["gift_gap"] = gift_gap_signal(
        gift, snapshot, prev_close, now, cfg["signals"]["gift_gap"],
        gift_error=gift_error, snapshot_problem=snapshot_problem,
    )
    signals["gift_gap"].detail["nifty_prev_close_source"] = prev_source

    signals["vix"] = _vix(ai, ai_error, payload, ai_result.fetched_at or now, cfg)

    cross_checks: dict[str, Any] = {
        "capital_market_status": capital_market.get("status") if capital_market else None,
        "capital_market_trade_date": capital_market["trade_date"].isoformat()
        if capital_market and capital_market.get("trade_date") else None,
    }
    for universe in ("NIFTY", "ALL"):
        result = nse_results[f"preopen_{universe}"]
        preopen, error = _parse(result, nse.parse_preopen, f"NSE pre-open {universe}")
        signals[f"breadth_{universe.lower()}"] = breadth_signal(
            universe, preopen, (result.fetched_at or now).time(), cfg["signals"]["breadth"], error=error
        )
        if preopen and preopen.get("timestamp"):
            stamp = preopen["timestamp"]
            cross_checks[f"preopen_{universe}_timestamp"] = stamp.isoformat()
            if stamp.date() != today:
                # Yesterday's book is not a cue for today.
                signals[f"breadth_{universe.lower()}"] = failed(
                    f"breadth_{universe.lower()}", f"NSE pre-open data is from {stamp:%d %b}"
                )

    composite = composite_bias(signals, cfg["composite"], scored_days)

    context = None
    if cfg["yahoo"].get("context_enabled"):
        context = {}
        for ticker, display in cfg["yahoo"]["context"].items():
            change = yahoo.daily_change(yahoo.bars_for(payload, ticker))
            context[display] = change["pct"] if change else None
    return MorningResult(signals=signals, composite=composite, context=context, cross_checks=cross_checks)


def _trim_preopen(raw: Any) -> Any:
    """Drop each stock's order-book ``detail`` before storing (it is ~95 % of the payload)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
        return raw
    return {**raw, "data": [{k: v for k, v in row.items() if k != "detail"} if isinstance(row, dict) else row
                            for row in raw["data"]]}


def main(ctx: RunContext) -> int:
    cfg = ctx.cfg
    nse_client = ctx.nse()
    try:
        calendar = trading_calendar(ctx, nse_client)
        if skip_if_closed(ctx, calendar):
            return 0

        yahoo_job = Background("yahoo", ctx.yahoo().fetch, ctx.clock)
        nse_results = {name: fetch_source(nse_client, name, ctx.clock) for name in NSE_SOURCES}
        reserve = float(cfg["run"]["reserve_seconds"])
        yahoo_result = yahoo_job.result(ctx.deadline.remaining() - reserve)
    finally:
        nse_client.close()

    snapshot = ctx.store.latest_outcome_before(ctx.trade_date)
    result = compute_morning(
        ctx.clock(), cfg, nse_results, yahoo_result, snapshot, calendar, ctx.store.scored_days(),
    )
    signals, composite = result.signals, result.composite

    # -- persist -------------------------------------------------------------
    for name, source in {**nse_results, "yahoo": yahoo_result}.items():
        payload = _trim_preopen(source.data) if name.startswith("preopen") else source.data
        ctx.store.save_raw(ctx.trade_date, "morning", name, ok=source.ok, payload=payload,
                           error=source.error, fetched_at=source.fetched_at)
    rows = [s.to_dict() for s in signals.values()]
    rows.append({"name": "composite_bias", "ok": composite.score is not None, "label": composite.bias,
                 "value": composite.score, "reason": None if composite.score is not None else "no directional inputs",
                 "flags": [] if composite.calibrated else ["uncalibrated"], "detail": composite.to_dict()})
    ctx.store.save_signals(ctx.trade_date, rows)

    # -- optional LLM summary ---------------------------------------------------
    summary = None
    if not ctx.dry_run and ctx.deadline.remaining() > float(cfg["llm"]["timeout_seconds"]) + 5:
        summary = llm.summarize(
            llm.llm_input({s["name"]: s for s in rows if s["name"] != "composite_bias"}, composite.to_dict()),
            ctx.trade_date.isoformat(), ctx.settings, cfg, ctx.data_dir / "llm_state.json",
        )

    lines = notify.brief_lines(ctx.started, signals, composite, context=result.context, summary=summary)
    delivery = deliver(ctx, lines)

    failures = {name: s.reason for name, s in signals.items() if not s.ok}
    record = {
        "run_id": ctx.run_id,
        "job": "morning",
        "trade_date": ctx.trade_date.isoformat(),
        "started_at": ctx.started.isoformat(),
        "finished_at": ctx.clock().isoformat(),
        "status": "ok" if not failures else "partial",
        "sources_ok": composite.sources_ok,
        "sources_total": composite.sources_total,
        "detail": {
            "delivery": delivery,
            "calendar": calendar.source,
            "nse_requests": getattr(nse_client, "requests_made", None),
            "failures": failures,
            "cross_checks": result.cross_checks,
            "llm_summary": bool(summary),
        },
    }
    ctx.store.save_run(ctx.trade_date, "morning", record)
    ctx.jsonl({**record, "signals": rows, "brief": lines})
    ctx.log.info("brief %s", delivery, extra={"data": record["detail"]})
    return finish(ctx, delivery)


if __name__ == "__main__":
    sys.exit(run_job("morning", main))
