"""End-to-end job runs against fixtures: no network, Telegram captured."""

import copy
import json
import sqlite3
from datetime import timedelta

import httpx
import pytest

from preopen_brief.jobs import close, common, morning, weekly
from preopen_brief.jobs.common import run_job
from preopen_brief.retry import SourceResult
from preopen_brief.sources.nse import NseClient
from preopen_brief.store import Store

from .conftest import FIXTURES, MORNING_NOW, ist, load_fixture

PREV_CLOSE = ["--offline", str(FIXTURES / "close_prev"), "--now", "2026-11-12T15:32:10"]
MORNING = ["--offline", str(FIXTURES / "morning"), "--now", "2026-11-13T09:09:05"]
CLOSE = ["--offline", str(FIXTURES / "close"), "--now", "2026-11-13T15:32:10"]


def run(job, main, argv, tmp_path, *extra):
    return run_job(job, main, [*argv, "--data-dir", str(tmp_path), *extra])


def nse_results(now=MORNING_NOW, **overrides):
    names = ("marketStatus", "preopen_NIFTY", "preopen_ALL", "allIndices")
    results = {n: SourceResult(n, True, load_fixture("morning", n), fetched_at=now) for n in names}
    results.update(overrides)
    return results


def yahoo_result(payload=None):
    return SourceResult("yahoo", True, payload or load_fixture("morning", "yahoo"), fetched_at=MORNING_NOW)


SNAPSHOT = {"trade_date": "2026-11-12", "gift_last": 25948.0, "gift_expiry": "2026-11-26",
            "nifty_close": 25875.80, "gift_fresh": True}


# -- acceptance: dry run prints the brief without sending ------------------------

def test_morning_dry_run_prints_brief_and_sends_nothing(tmp_path, capsys, outbox):
    assert run("morning", morning.main, MORNING, tmp_path, "--dry-run") == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "Pre-open brief · 2026-11-13 09:09 IST",
        "US (prev close): S&P +0.42% | Nasdaq +0.61% | Dow +0.18% -> positive",
        "US futures: ES +0.12% | NQ +0.20% | YM +0.05% -> flat",
        "GIFT Nifty: 26,030.00 | est. gap +0.30% (unadjusted) -> positive",   # no prior snapshot yet
        "India VIX: 13.42 (prev close, 16th pct, -1.18%) -> stable",
        "Breadth NIFTY50 pre-open: 31 adv / 17 dec / 2 unch (ratio 1.82) -> positive",
        "Breadth ALL pre-open: 1040 adv / 890 dec / 95 unch (ratio 1.17) -> flat",
        "Bias: positive · completeness 6/6 · uncalibrated",
        "Not investment advice.",
    ]
    assert outbox["sent"] == [] and outbox["pings"] == []


def test_full_cycle_close_morning_close_weekly(tmp_path, capsys, outbox):
    assert run("close", close.main, PREV_CLOSE, tmp_path) == 0
    assert outbox["sent"] == []                                       # close.notify is false by default
    assert run("morning", morning.main, MORNING, tmp_path) == 0
    brief = outbox["sent"][-1]
    assert brief.startswith("<b>Pre-open brief · 2026-11-13 09:09 IST</b>")
    assert "est. gap +0.32% (basis-adjusted) -&gt; positive" in brief   # uses Thursday's 15:32 snapshot
    assert brief.endswith("Not investment advice.")
    assert run("close", close.main, CLOSE, tmp_path) == 0

    store = Store(tmp_path / "preopen.db")
    outcome = store.get_outcome("2026-11-13")
    assert outcome["realized_gap"] == pytest.approx((25951.20 / 25875.80 - 1) * 100)
    assert outcome["day_return"] == pytest.approx((25968.40 / 25875.80 - 1) * 100)
    assert (outcome["gift_last"], outcome["gift_expiry"], outcome["gift_fresh"]) == (26041.0, "2026-11-26", True)
    scored = {s["name"]: s for s in store.get_signals("2026-11-13")}
    assert scored["gift_gap"]["hit_gap"] == 1
    assert scored["gift_gap"]["abs_error"] == pytest.approx(abs(0.3169 - 0.2914), abs=1e-3)
    assert scored["us_futures"]["hit_gap"] is None                    # flat: excluded from scoring
    assert scored["vix"]["hit_gap"] is None                           # no direction
    assert scored["composite_bias"]["label"] == "positive" and scored["composite_bias"]["hit_gap"] == 1
    assert store.scored_days() == 1
    store.close()

    assert run("weekly", weekly.main, [*CLOSE[:2], "--now", "2026-11-14T10:00:00"], tmp_path) == 0
    report = outbox["sent"][-1]
    assert report.startswith("<b>Weekly evaluation · 2026-11-14</b>\n<pre>")
    assert "uncalibrated (1/60 scored days)" in report and "Weights: not tuned" in report
    assert report.endswith("Not investment advice.")
    assert [ok for _, ok in outbox["pings"]] == [True, True, True, True]

    lines = [json.loads(line) for line in (tmp_path / "runs.jsonl").read_text().splitlines()]
    assert [(r["job"], r["trade_date"]) for r in lines] == [
        ("close", "2026-11-12"), ("morning", "2026-11-13"), ("close", "2026-11-13"), ("weekly", "2026-11-14")]
    assert all(len(r["run_id"]) == 12 for r in lines)


def test_rerun_same_day_is_idempotent(tmp_path, outbox):
    for _ in range(2):
        assert run("morning", morning.main, MORNING, tmp_path) == 0
    db = sqlite3.connect(tmp_path / "preopen.db")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 7      # 6 signals + composite
    assert db.execute("SELECT COUNT(*) FROM raw_snapshots").fetchone()[0] == 5
    stored = json.loads(db.execute(
        "SELECT payload FROM raw_snapshots WHERE source = 'preopen_NIFTY'").fetchone()[0])
    assert "detail" not in stored["data"][0] and "metadata" in stored["data"][0]   # order book trimmed
    assert len((tmp_path / "runs.jsonl").read_text().splitlines()) == 2        # the log is append-only


# -- edge: holiday / weekend -----------------------------------------------------

@pytest.mark.parametrize("now,reason", [("2026-12-25T09:09:00", "holiday: Christmas"), ("2026-11-14T09:09:00", "weekend")])
def test_non_trading_day_exits_quietly(tmp_path, capsys, outbox, now, reason):
    argv = ["--offline", str(FIXTURES / "morning"), "--now", now]
    assert run("morning", morning.main, argv, tmp_path) == 0
    assert run("close", close.main, argv, tmp_path) == 0
    assert outbox["sent"] == [] and capsys.readouterr().out == ""
    assert [ok for _, ok in outbox["pings"]] == [True, True]          # dead-man's switch still fed
    db = sqlite3.connect(tmp_path / "preopen.db")
    assert db.execute("SELECT status, detail FROM runs").fetchall() == [
        ("skipped", json.dumps({"reason": reason}))] * 2
    assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


def test_force_runs_on_a_holiday(tmp_path, capsys):
    argv = ["--offline", str(FIXTURES / "morning"), "--now", "2026-12-25T09:09:00", "--dry-run"]
    assert run("morning", morning.main, argv, tmp_path) == 0
    assert "not a trading day (holiday: Christmas)" in capsys.readouterr().out
    assert run("morning", morning.main, argv, tmp_path, "--force") == 0
    assert capsys.readouterr().out.startswith("Pre-open brief · 2026-12-25")


# -- edge: NSE 403 ---------------------------------------------------------------

def test_nse_403_gives_partial_brief_with_reasons(tmp_path, monkeypatch, outbox):
    requests = []

    def blocked(request):
        requests.append(request.url.path)
        return httpx.Response(403, text="Access Denied")

    monkeypatch.setattr(common.RunContext, "nse", lambda self: NseClient(
        self.cfg, transport=httpx.MockTransport(blocked), sleep=lambda s: None))
    assert run("morning", morning.main, MORNING, tmp_path) == 0
    lines = outbox["sent"][-1].split("\n")
    assert lines[1].endswith("-&gt; positive") and lines[2].endswith("-&gt; flat")   # Yahoo unaffected
    assert lines[3] == "GIFT Nifty: n/a (NSE marketStatus: HTTP 403 after cookie refresh)"
    assert lines[4].startswith("India VIX: 13.42 (prev close, yahoo, ")               # ^INDIAVIX fallback
    assert lines[5] == "Breadth NIFTY50 pre-open: n/a (NSE pre-open NIFTY: HTTP 403 after cookie refresh)"
    assert lines[6] == "Breadth ALL pre-open: n/a (NSE pre-open ALL: HTTP 403 after cookie refresh)"
    assert lines[7] == "Bias: positive · completeness 3/6 · uncalibrated"
    assert lines[8] == "Not investment advice."
    # home, api, home (single refresh), api - then fail fast for the other endpoints
    assert requests == ["/", "/api/holiday-master", "/", "/api/holiday-master"]
    run_row = sqlite3.connect(tmp_path / "preopen.db").execute("SELECT status, sources_ok FROM runs").fetchone()
    assert run_row == ("partial", 3)


# -- edge: partial data / compute_morning ------------------------------------------

def test_basis_adjusted_gap_and_contract_roll(cfg, calendar):
    result = morning.compute_morning(MORNING_NOW, cfg, nse_results(), yahoo_result(), SNAPSHOT, calendar, 0)
    gift = result.signals["gift_gap"]
    assert gift.flags == ["basis-adjusted"] and gift.value == pytest.approx(82.0 / 25875.80 * 100)
    assert gift.detail["nifty_prev_close_source"] == "close snapshot"
    rolled = {**SNAPSHOT, "gift_expiry": "2026-10-29"}
    gift = morning.compute_morning(MORNING_NOW, cfg, nse_results(), yahoo_result(), rolled, calendar, 0).signals["gift_gap"]
    assert gift.flags == ["unadjusted"] and gift.detail["fallback_reason"] == "contract roll" and gift.value == 0.30


def test_snapshot_from_an_older_session_is_not_used(cfg, calendar):
    old = {**SNAPSHOT, "trade_date": "2026-11-11"}      # Thursday the 12th traded in between
    gift = morning.compute_morning(MORNING_NOW, cfg, nse_results(), yahoo_result(), old, calendar, 0).signals["gift_gap"]
    assert gift.flags == ["unadjusted"] and "no snapshot for previous session" in gift.detail["fallback_reason"]
    assert gift.detail["nifty_prev_close_source"] == "allIndices.last"
    # ...but a snapshot from before a holiday/weekend is still "the previous session"
    monday = ist("2026-11-16T09:09:05")
    ms = load_fixture("morning", "marketStatus")
    ms["giftnifty"]["TIMESTMP"] = "16-Nov-2026 09:08"
    friday = {**SNAPSHOT, "trade_date": "2026-11-13"}
    results = nse_results(monday, marketStatus=SourceResult("marketStatus", True, ms, fetched_at=monday))
    gift = morning.compute_morning(monday, cfg, results, yahoo_result(), friday, calendar, 0).signals["gift_gap"]
    assert gift.flags == ["basis-adjusted"]


def test_stale_gift_timestamp_shows_na(cfg, calendar):
    ms = load_fixture("morning", "marketStatus")
    ms["giftnifty"]["TIMESTMP"] = "13-Nov-2026 08:40"
    results = nse_results(marketStatus=SourceResult("marketStatus", True, ms, fetched_at=MORNING_NOW))
    result = morning.compute_morning(MORNING_NOW, cfg, results, yahoo_result(), SNAPSHOT, calendar, 0)
    assert not result.signals["gift_gap"].ok
    assert result.signals["gift_gap"].reason == "stale GIFT quote, 08:40 IST"
    assert "gift_gap" not in result.composite.contributions and result.composite.sources_ok == 5


def test_missing_yfinance_ticker_and_yahoo_outage(cfg, calendar):
    payload = load_fixture("morning", "yahoo")
    payload["daily"]["^DJI"] = []
    payload["intraday"]["NQ=F"] = []
    result = morning.compute_morning(MORNING_NOW, cfg, nse_results(), yahoo_result(payload), SNAPSHOT, calendar, 0)
    cash, futures = result.signals["us_cash"], result.signals["us_futures"]
    assert cash.ok and cash.detail["components"]["Dow"] is None and cash.flags == ["partial: no Dow"]
    assert cash.value == pytest.approx((0.42 + 0.61) / 2, abs=1e-3)
    assert futures.ok and futures.detail["components"]["NQ"] is None

    down = SourceResult.failed("yahoo", "yfinance: YFRateLimitError", MORNING_NOW)
    result = morning.compute_morning(MORNING_NOW, cfg, nse_results(), down, SNAPSHOT, calendar, 0)
    assert result.signals["us_cash"].reason == "yahoo: yfinance: YFRateLimitError"
    assert not result.signals["us_futures"].ok
    vix = result.signals["vix"]
    assert vix.ok and vix.value == 13.42 and vix.detail["percentile"] is None    # NSE level, no history
    assert result.signals["gift_gap"].ok and result.composite.sources_ok == 4
    assert result.composite.bias == "positive"                                   # GIFT + breadth still decide


def test_intraday_outage_falls_back_to_daily_bars(cfg, calendar):
    payload = load_fixture("morning", "yahoo")
    payload["intraday"], payload["errors"] = {}, {"intraday": "yfinance: empty response"}
    futures = morning.compute_morning(MORNING_NOW, cfg, nse_results(), yahoo_result(payload), None, calendar, 0).signals["us_futures"]
    assert futures.ok and futures.flags == ["daily-bar ref"]
    assert futures.value == pytest.approx((0.40 + 0.55 + 0.20) / 3, abs=1e-3)


def test_us_holiday_makes_us_cash_stale(cfg, calendar):
    """US closed Thursday 12 Nov (hypothetically): Wednesday's move was already traded in India."""
    payload = load_fixture("morning", "yahoo")
    for ticker in cfg["yahoo"]["us_cash"]:
        payload["daily"][ticker] = payload["daily"][ticker][:-1]      # last bar is now Wed 11 Nov
    cash = morning.compute_morning(MORNING_NOW, cfg, nse_results(), yahoo_result(payload), None, calendar, 0).signals["us_cash"]
    assert not cash.ok and cash.reason == "stale: last US session 11 Nov"
    # Monday morning after a normal Friday session is NOT stale (only a weekend in between).
    monday = ist("2026-11-16T09:09:05")
    cash = morning.compute_morning(monday, cfg, nse_results(monday), yahoo_result(), None, calendar, 0).signals["us_cash"]
    assert not cash.ok   # Thursday's bar is stale by Monday (Friday traded in between)...
    payload = load_fixture("morning", "yahoo")
    for ticker in cfg["yahoo"]["us_cash"]:
        last = payload["daily"][ticker][-1]
        payload["daily"][ticker].append({**last, "t": "2026-11-13"})
    cash = morning.compute_morning(monday, cfg, nse_results(monday), yahoo_result(payload), None, calendar, 0).signals["us_cash"]
    assert cash.ok       # ...while Friday's bar is fresh on Monday


def test_yesterdays_preopen_book_is_rejected(cfg, calendar):
    po = load_fixture("morning", "preopen_NIFTY")
    po["timestamp"] = "12-Nov-2026 09:08:01"
    results = nse_results(preopen_NIFTY=SourceResult("preopen_NIFTY", True, po, fetched_at=MORNING_NOW))
    result = morning.compute_morning(MORNING_NOW, cfg, results, yahoo_result(), SNAPSHOT, calendar, 0)
    assert result.signals["breadth_nifty"].reason == "NSE pre-open data is from 12 Nov"
    assert result.signals["breadth_all"].ok


def test_breadth_indicative_when_fetched_before_0908(cfg, calendar):
    early = ist("2026-11-13T09:07:30")
    result = morning.compute_morning(early, cfg, nse_results(early), yahoo_result(), None, calendar, 0)
    assert result.signals["breadth_nifty"].flags == ["indicative"]


def test_context_line_behind_flag(cfg, calendar):
    assert morning.compute_morning(MORNING_NOW, cfg, nse_results(), yahoo_result(), None, calendar, 0).context is None
    cfg["yahoo"]["context_enabled"] = True
    payload = load_fixture("morning", "yahoo")
    payload["daily"]["^N225"] = [{"t": "2026-11-12", "c": 100.0}, {"t": "2026-11-13", "c": 101.0}]
    context = morning.compute_morning(MORNING_NOW, cfg, nse_results(), yahoo_result(payload), None, calendar, 0).context
    assert context["Nikkei"] == pytest.approx(1.0) and context["Brent"] is None


def test_unexpected_response_shape_is_isolated(cfg, calendar):
    results = nse_results(allIndices=SourceResult("allIndices", True, {"data": "surprise"}, fetched_at=MORNING_NOW),
                          preopen_ALL=SourceResult("preopen_ALL", True, ["not", "a", "dict"], fetched_at=MORNING_NOW))
    result = morning.compute_morning(MORNING_NOW, cfg, results, yahoo_result(), SNAPSHOT, calendar, 0)
    assert result.signals["vix"].ok and result.signals["vix"].flags == ["prev close", "yahoo"]
    assert result.signals["breadth_all"].reason == "pre-open: response is not an object"
    assert result.signals["breadth_nifty"].ok and result.signals["gift_gap"].ok


# -- close job -------------------------------------------------------------------

def test_close_falls_back_to_yahoo_nsei(tmp_path, monkeypatch, outbox):
    class StaleIndices(common.FixtureNse):
        def fetch(self, name):
            data = super().fetch(name)
            if name == "allIndices":
                data["timestamp"] = "12-Nov-2026 15:30"          # NSE still serving yesterday
            return data

    monkeypatch.setattr(common.RunContext, "nse", lambda self: StaleIndices(self.offline_dir))
    assert run("close", close.main, CLOSE, tmp_path) == 0
    outcome = Store(tmp_path / "preopen.db").get_outcome("2026-11-13")
    assert outcome["nifty_source"] == "yahoo ^NSEI"
    assert (outcome["nifty_open"], outcome["nifty_close"], outcome["nifty_prev_close"]) == (25951.2, 25968.4, 25875.8)


def test_close_with_stale_gift_marks_snapshot_unusable(tmp_path, monkeypatch, outbox):
    class StaleGift(common.FixtureNse):
        def fetch(self, name):
            data = super().fetch(name)
            if name == "marketStatus":
                data["giftnifty"]["TIMESTMP"] = "13-Nov-2026 14:55"
            return data

    monkeypatch.setattr(common.RunContext, "nse", lambda self: StaleGift(self.offline_dir))
    assert run("close", close.main, CLOSE, tmp_path) == 0
    store = Store(tmp_path / "preopen.db")
    assert store.get_outcome("2026-11-13")["gift_fresh"] is False
    status, detail = store._db.execute("SELECT status, detail FROM runs").fetchone()
    assert status == "partial" and "GIFT quote stale at close" in detail


def test_close_without_any_nifty_source_alerts(tmp_path, monkeypatch, outbox):
    class Broken(common.FixtureNse):
        def fetch(self, name):
            if name == "allIndices":
                raise common.SourceError("HTTP 503")
            return super().fetch(name)

    class NoYahoo:
        def fetch(self, **kwargs):
            raise common.SourceError("yfinance: empty response")

    monkeypatch.setattr(common.RunContext, "nse", lambda self: Broken(self.offline_dir))
    monkeypatch.setattr(common.RunContext, "yahoo", lambda self: NoYahoo())
    assert run("close", close.main, CLOSE, tmp_path) == 0
    alert = outbox["sent"][-1]
    assert "Close job 2026-11-13: incomplete" in alert and "NSE allIndices: HTTP 503" in alert
    outcome = Store(tmp_path / "preopen.db").get_outcome("2026-11-13")
    assert outcome["realized_gap"] is None and outcome["gift_last"] == 26041.0   # GIFT snapshot still saved


# -- failure handling ------------------------------------------------------------

def test_unhandled_exception_sends_short_alert_with_run_id(tmp_path, monkeypatch, outbox):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom <html> 123456:TEST-TOKEN")

    monkeypatch.setattr(morning, "compute_morning", boom)
    assert run("morning", morning.main, MORNING, tmp_path) == 1
    alert = outbox["sent"][-1]
    assert alert.startswith("preopen-brief morning job failed · run_id ")
    run_id = alert.split("run_id ")[1].split(" ")[0]
    assert len(run_id) == 12
    assert "RuntimeError: kaboom &lt;html&gt;" in alert and "TEST-TOKEN" not in alert
    assert outbox["pings"] == [("https://hc.example/ping/abc", False)]


def test_failed_telegram_send_returns_nonzero(tmp_path, monkeypatch, outbox):
    def fail(*args, **kwargs):
        raise common.SourceError("telegram: HTTP 401 Unauthorized")

    monkeypatch.setattr(common.notify, "send_telegram", fail)
    assert run("morning", morning.main, MORNING, tmp_path) == 1
    assert outbox["pings"] == [("https://hc.example/ping/abc", False)]
    detail = sqlite3.connect(tmp_path / "preopen.db").execute("SELECT detail FROM runs").fetchone()[0]
    assert "send failed: telegram: HTTP 401 Unauthorized" in detail              # signals are still stored


def test_run_budget_times_out_a_hung_source():
    import threading

    gate = threading.Event()
    job = common.Background("yahoo", lambda: gate.wait(5), lambda: MORNING_NOW)
    result = job.result(timeout=0.05)
    gate.set()
    assert not result.ok and result.error == "timed out (run budget)"
