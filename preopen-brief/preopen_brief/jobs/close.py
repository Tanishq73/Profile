"""15:32 IST close job: GIFT snapshot for tomorrow's basis + outcome scoring.

    python -m preopen_brief.jobs.close [--dry-run] [--offline DIR --now ISO]
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from typing import Any

from .. import notify
from ..evaluate import score_signals
from ..retry import SourceError, SourceResult
from ..sources import nse, yahoo
from .common import RunContext, deliver, fetch_source, finish, run_job, skip_if_closed, trading_calendar


def nifty_from_all_indices(result: SourceResult, today: date) -> dict[str, float]:
    """NIFTY 50 open / close / previous close from allIndices, only if it is today's session."""
    if not result.ok:
        raise SourceError(f"NSE allIndices: {result.error}")
    parsed = nse.parse_all_indices(result.data)
    stamp = parsed["timestamp"]
    if stamp is not None and stamp.date() != today:
        raise SourceError(f"NSE allIndices is from {stamp:%d %b}")
    row = nse.get_index(parsed, "NIFTY 50")
    values = {"open": row["open"], "close": row["last"], "prev_close": row["previous_close"]}
    missing = [k for k, v in values.items() if not v or v <= 0]
    if missing:
        raise SourceError(f"NSE allIndices: NIFTY 50 {'/'.join(missing)} missing")
    return values


def nifty_from_yahoo(bars: list[dict[str, Any]], today: date) -> dict[str, float]:
    """^NSEI fallback: today's daily bar and the close before it."""
    if len(bars) < 2 or date.fromisoformat(bars[-1]["t"]) != today:
        raise SourceError("yahoo ^NSEI: no bar for today")
    last, prev = bars[-1], bars[-2]
    if not last.get("o") or not last.get("c") or not prev.get("c"):
        raise SourceError("yahoo ^NSEI: incomplete bar")
    return {"open": last["o"], "close": last["c"], "prev_close": prev["c"]}


def build_outcome(
    now: datetime,
    nifty: dict[str, float] | None,
    nifty_source: str | None,
    gift: dict[str, Any] | None,
    max_quote_age_minutes: float,
) -> dict[str, Any]:
    """Outcome row: realized gap / returns (percent) and the GIFT close snapshot."""
    outcome: dict[str, Any] = {"recorded_at": now.isoformat(), "nifty_source": nifty_source}
    if nifty:
        outcome.update(
            nifty_open=nifty["open"],
            nifty_close=nifty["close"],
            nifty_prev_close=nifty["prev_close"],
            realized_gap=(nifty["open"] / nifty["prev_close"] - 1.0) * 100.0,
            day_return=(nifty["close"] / nifty["prev_close"] - 1.0) * 100.0,
            intraday_return=(nifty["close"] / nifty["open"] - 1.0) * 100.0,
        )
    if gift:
        stamp = gift.get("timestamp")
        fresh = stamp is not None and (now - stamp).total_seconds() / 60.0 <= max_quote_age_minutes
        outcome.update(
            gift_last=gift["last"],
            gift_expiry=gift.get("expiry"),
            gift_timestamp=stamp.isoformat() if stamp else None,
            gift_fresh=fresh,
        )
    return outcome


def main(ctx: RunContext) -> int:
    cfg = ctx.cfg
    today = ctx.trade_date
    problems: list[str] = []
    nse_client = ctx.nse()
    try:
        calendar = trading_calendar(ctx, nse_client)
        if skip_if_closed(ctx, calendar):
            return 0
        ms = fetch_source(nse_client, "marketStatus", ctx.clock)
        ai = fetch_source(nse_client, "allIndices", ctx.clock)
    finally:
        nse_client.close()
    sources = {"marketStatus": ms, "allIndices": ai}

    nifty: dict[str, float] | None = None
    nifty_source: str | None = None
    try:
        nifty, nifty_source = nifty_from_all_indices(ai, today), "allIndices"
    except SourceError as exc:
        problems.append(str(exc))
        try:  # only now is Yahoo worth a call
            payload = ctx.yahoo().fetch(daily_tickers=[cfg["yahoo"]["nifty"]], with_intraday=False)
            sources["yahoo"] = SourceResult(name="yahoo", ok=True, data=payload, fetched_at=ctx.clock())
            nifty, nifty_source = nifty_from_yahoo(yahoo.bars_for(payload, cfg["yahoo"]["nifty"]), today), "yahoo ^NSEI"
        except SourceError as yexc:
            problems.append(str(yexc))
            sources.setdefault("yahoo", SourceResult.failed("yahoo", str(yexc), ctx.clock()))

    gift = None
    try:
        if not ms.ok:
            raise SourceError(f"NSE marketStatus: {ms.error}")
        gift = nse.parse_gift(ms.data)
    except SourceError as exc:
        problems.append(str(exc))

    outcome = build_outcome(ctx.clock(), nifty, nifty_source, gift, float(cfg["signals"]["gift_gap"]["max_quote_age_minutes"]))
    if gift and not outcome.get("gift_fresh"):
        problems.append("GIFT quote stale at close; tomorrow's gap will be unadjusted")

    ctx.store.save_outcome(today, outcome)
    for name, source in sources.items():
        ctx.store.save_raw(today, "close", name, ok=source.ok, payload=source.data, error=source.error,
                           fetched_at=source.fetched_at)
    morning = ctx.store.get_signals(today)
    scores = score_signals(morning, outcome.get("realized_gap"), outcome.get("day_return")) if nifty else []
    if scores:
        ctx.store.save_scores(today, scores)
    elif nifty and not morning:
        problems.append("no morning signals to score")

    if ctx.dry_run or cfg["close"]["notify"]:
        delivery = deliver(ctx, notify.close_lines(today, outcome, scores))
    elif nifty is None or gift is None:
        delivery = deliver(ctx, [f"Close job {today}: incomplete · run_id {ctx.run_id}", *problems, notify.DISCLAIMER])
    else:
        delivery = "not sent (close.notify is false)"

    record = {
        "run_id": ctx.run_id,
        "job": "close",
        "trade_date": today.isoformat(),
        "started_at": ctx.started.isoformat(),
        "finished_at": ctx.clock().isoformat(),
        "status": "ok" if nifty and gift and outcome.get("gift_fresh") else "partial",
        "sources_ok": sum(1 for s in sources.values() if s.ok),
        "sources_total": len(sources),
        "detail": {"delivery": delivery, "problems": problems, "calendar": calendar.source,
                   "nse_requests": getattr(nse_client, "requests_made", None)},
    }
    ctx.store.save_run(today, "close", record)
    ctx.jsonl({**record, "outcome": outcome, "scores": scores})
    ctx.log.info("close snapshot stored", extra={"data": record["detail"]})
    return finish(ctx, delivery)


if __name__ == "__main__":
    sys.exit(run_job("close", main))
