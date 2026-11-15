"""Plumbing shared by the morning, close and weekly jobs."""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import notify
from ..config import IST, RunLogger, Settings, load_config, load_settings, resolve_path, setup_logging
from ..retry import Deadline, SourceError, SourceResult
from ..sources.nse import FixtureNse, NseClient
from ..sources.yahoo import FixtureYahoo, YahooSource
from ..store import Store, append_jsonl
from ..tradingday import Calendar, load_calendar


@dataclass
class RunContext:
    job: str
    run_id: str
    started: datetime            # aware IST; simulated when --now is given
    cfg: dict[str, Any]
    settings: Settings
    data_dir: Path
    store: Store
    deadline: Deadline
    dry_run: bool
    force: bool
    offline_dir: Path | None
    simulated: bool
    log: logging.LoggerAdapter
    out: Callable[[str], None] = print

    @property
    def trade_date(self):
        return self.started.date()

    def clock(self) -> datetime:
        """Current IST time (frozen at --now when simulating)."""
        return self.started if self.simulated else datetime.now(IST)

    def nse(self) -> Any:
        return FixtureNse(self.offline_dir) if self.offline_dir else NseClient(self.cfg, deadline=self.deadline)

    def yahoo(self) -> Any:
        return FixtureYahoo(self.offline_dir) if self.offline_dir else YahooSource(self.cfg)

    def jsonl(self, record: dict[str, Any]) -> None:
        append_jsonl(self.data_dir / "runs.jsonl", record)


def parse_args(job: str, argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=f"python -m preopen_brief.jobs.{job}", description=f"preopen-brief {job} job")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram; no LLM call, no healthcheck ping")
    parser.add_argument("--force", action="store_true", help="run even if today is not a trading day")
    parser.add_argument("--offline", metavar="DIR", help="read saved fixtures from DIR instead of the network (implies a throwaway data dir)")
    parser.add_argument("--now", metavar="ISO", help="pretend the current IST time is this (e.g. 2026-11-13T09:09:05)")
    parser.add_argument("--data-dir", metavar="DIR", help="override paths.data_dir")
    parser.add_argument("--config", metavar="FILE", help="path to config.yaml")
    if job == "weekly":
        parser.add_argument("--days", type=int, help="evaluation window (default: evaluation.weekly_days)")
    return parser.parse_args(argv)


def build_context(job: str, args: argparse.Namespace, *, settings: Settings | None = None) -> RunContext:
    cfg = load_config(args.config)
    settings = settings or load_settings()
    if args.now:
        started = datetime.fromisoformat(args.now)
        started = started.replace(tzinfo=IST) if started.tzinfo is None else started.astimezone(IST)
    else:
        started = datetime.now(IST)
    offline_dir = Path(args.offline) if args.offline else None
    if args.data_dir:
        data_dir = resolve_path(cfg, "data_dir", args.data_dir)
    elif offline_dir:
        data_dir = Path(tempfile.mkdtemp(prefix="preopen-offline-"))
    else:
        data_dir = resolve_path(cfg, "data_dir")
    logs_dir = None if offline_dir else resolve_path(cfg, "logs_dir")
    run_id = uuid.uuid4().hex[:12]
    logger = setup_logging(cfg, settings, logs_dir)
    return RunContext(
        job=job,
        run_id=run_id,
        started=started,
        cfg=cfg,
        settings=settings,
        data_dir=data_dir,
        store=Store(data_dir / "preopen.db"),
        deadline=Deadline(float(cfg["run"]["budget_seconds"])),
        dry_run=args.dry_run,
        force=args.force,
        offline_dir=offline_dir,
        simulated=bool(args.now),
        log=RunLogger(logger, {"run_id": run_id, "job": job}),
    )


def fetch_source(client: Any, name: str, clock: Callable[[], datetime]) -> SourceResult:
    """Fetch one NSE endpoint; a failure becomes a SourceResult, never an exception."""
    try:
        data = client.fetch(name)
    except SourceError as exc:
        return SourceResult.failed(name, str(exc), clock())
    except Exception as exc:  # noqa: BLE001 - isolate each source completely
        return SourceResult.failed(name, f"{type(exc).__name__}", clock())
    return SourceResult(name=name, ok=True, data=data, fetched_at=clock())


class Background:
    """Run one fetch on a daemon thread so a hung library cannot outlive the run budget."""

    def __init__(self, name: str, fn: Callable[[], Any], clock: Callable[[], datetime]):
        self._name, self._clock = name, clock
        self._queue: queue.Queue[SourceResult] = queue.Queue(maxsize=1)
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            result = SourceResult(name=self._name, ok=True, data=fn(), fetched_at=self._clock())
        except SourceError as exc:
            result = SourceResult.failed(self._name, str(exc), self._clock())
        except Exception as exc:  # noqa: BLE001
            result = SourceResult.failed(self._name, f"{type(exc).__name__}", self._clock())
        self._queue.put(result)

    def result(self, timeout: float) -> SourceResult:
        try:
            return self._queue.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            return SourceResult.failed(self._name, "timed out (run budget)", self._clock())


def trading_calendar(ctx: RunContext, nse_client: Any) -> Calendar:
    """Holiday calendar for today; spends one NSE request only when the cache needs it."""
    if ctx.trade_date.weekday() >= 5:
        return Calendar(None, "weekend")
    return load_calendar(ctx.started, ctx.data_dir, ctx.cfg, lambda: nse_client.fetch("holiday_master"))


def skip_if_closed(ctx: RunContext, calendar: Calendar) -> bool:
    """Log and return True when today is not a trading day (and --force is absent)."""
    reason = calendar.why_closed(ctx.trade_date)
    if reason is None or ctx.force:
        return False
    ctx.log.info("not a trading day (%s); exiting quietly", reason)
    record = {"run_id": ctx.run_id, "started_at": ctx.started.isoformat(), "status": "skipped", "detail": {"reason": reason}}
    ctx.store.save_run(ctx.trade_date, ctx.job, record)
    if ctx.dry_run:
        ctx.out(f"{ctx.trade_date} is not a trading day ({reason}); nothing to do.")
    elif ctx.cfg["healthcheck"]["ping_on_skip"]:
        notify.ping_healthcheck(ctx.settings.healthcheck_url(ctx.job), ctx.cfg)
    return True


def deliver(ctx: RunContext, lines: list[str] | None = None, *, html: str | None = None, plain: str | None = None) -> str:
    """Print (dry run) or send to Telegram. Returns "printed", "sent" or "send failed: ..."."""
    if ctx.dry_run:
        ctx.out(plain if plain is not None else "\n".join(lines or []))
        return "printed"
    try:
        notify.send_telegram(html if html is not None else notify.to_html(lines or []), ctx.settings, ctx.cfg)
    except SourceError as exc:
        ctx.log.error("delivery failed: %s", exc)
        return f"send failed: {exc}"
    return "sent"


def finish(ctx: RunContext, delivery: str) -> int:
    """Ping the dead-man's switch after a successful run; exit code 0/1."""
    ok = not delivery.startswith("send failed")
    if not ctx.dry_run:
        notify.ping_healthcheck(ctx.settings.healthcheck_url(ctx.job), ctx.cfg, ok=ok)
    return 0 if ok else 1


def run_job(job: str, main: Callable[[RunContext], int], argv: list[str] | None = None) -> int:
    """Entry point wrapper: any unhandled exception becomes a short alert with the run_id."""
    args = parse_args(job, argv)
    ctx: RunContext | None = None
    try:
        ctx = build_context(job, args)
        ctx.log.info("run started", extra={"data": {"dry_run": ctx.dry_run, "offline": bool(ctx.offline_dir)}})
        code = main(ctx)
        ctx.log.info("run finished", extra={"data": {"exit_code": code}})
        return code
    except Exception as exc:  # noqa: BLE001 - last line of defence
        run_id = ctx.run_id if ctx else "unknown"
        error = f"{type(exc).__name__}: {exc}"
        if ctx is None:
            print(f"preopen-brief {job} failed before start: {error}", file=sys.stderr)
            return 1
        ctx.log.exception("unhandled exception")
        scrubbed = error
        for secret in ctx.settings.secrets():
            scrubbed = scrubbed.replace(secret, "***")
        if ctx.dry_run:
            print(f"ALERT (not sent, dry run): {job} job failed · run_id {run_id} · {scrubbed}", file=sys.stderr)
        else:
            try:
                notify.send_telegram(notify.alert_html(job, run_id, scrubbed), ctx.settings, ctx.cfg)
            except SourceError as send_exc:
                ctx.log.error("could not send alert: %s", send_exc)
            notify.ping_healthcheck(ctx.settings.healthcheck_url(job), ctx.cfg, ok=False)
        return 1
    finally:
        if ctx is not None:
            ctx.store.close()
