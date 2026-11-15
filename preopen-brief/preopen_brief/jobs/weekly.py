"""Saturday 10:00 IST: weekly evaluation report to Telegram.

    python -m preopen_brief.jobs.weekly [--dry-run] [--days N]

Runs on a non-trading day by design, so it skips the trading-day check and
makes no NSE or Yahoo requests at all.
"""

from __future__ import annotations

import sys
from datetime import timedelta

from .. import notify
from ..evaluate import build_report, format_report
from .common import RunContext, deliver, finish, run_job


def make_report(ctx: RunContext, days: int) -> dict:
    since = ctx.trade_date - timedelta(days=days)
    return build_report(ctx.store.evaluation_rows(since), ctx.store.scored_days(), days, since, ctx.cfg)


def main(ctx: RunContext, days: int | None = None) -> int:
    days = days or int(ctx.cfg["evaluation"]["weekly_days"])
    report = make_report(ctx, days)
    text = format_report(report)
    title = f"Weekly evaluation · {ctx.trade_date}"
    delivery = deliver(ctx, html=notify.preformatted_html(title, text),
                       plain=f"{title}\n{text}\n{notify.DISCLAIMER}")
    record = {
        "run_id": ctx.run_id,
        "job": "weekly",
        "trade_date": ctx.trade_date.isoformat(),
        "started_at": ctx.started.isoformat(),
        "finished_at": ctx.clock().isoformat(),
        "status": "ok",
        "detail": {"delivery": delivery, "days": days, "scored_days": report["total_scored_days"]},
    }
    ctx.store.save_run(ctx.trade_date, "weekly", record)
    ctx.jsonl({**record, "report": report})
    return finish(ctx, delivery)


def _entry(argv: list[str] | None = None) -> int:
    from .common import parse_args

    days = parse_args("weekly", argv).days
    return run_job("weekly", lambda ctx: main(ctx, days), argv)


if __name__ == "__main__":
    sys.exit(_entry())
