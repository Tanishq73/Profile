"""Statistics, scoring, the report, and SQLite idempotency."""

import json
import math
from datetime import date

import pytest

from preopen_brief import evaluate as ev
from preopen_brief.store import Store, append_jsonl


def test_wilson_interval_known_values():
    lo, hi = ev.wilson_interval(8, 10)
    assert (lo, hi) == (pytest.approx(0.4902, abs=1e-4), pytest.approx(0.9433, abs=1e-4))
    assert ev.wilson_interval(0, 0) is None
    lo, hi = ev.wilson_interval(0, 5)
    assert lo == 0.0 and 0 < hi < 0.5
    assert ev.wilson_interval(5, 5)[1] == 1.0


def test_pearson_and_spearman():
    xs, ys = [1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.5]
    assert ev.pearson(xs, ys) == pytest.approx(10.75 / math.sqrt(5 * 23.1875))  # worked by hand
    assert ev.spearman(xs, [1.0, 8.0, 27.0, 64.0]) == pytest.approx(1.0)   # monotonic, non-linear
    assert ev.spearman([1, 2, 2, 4], [1, 3, 3, 9]) == pytest.approx(1.0)   # ties get average ranks
    assert ev.spearman(xs, list(reversed(ys))) == pytest.approx(-1.0)
    assert ev.pearson([1.0, 2.0], [1.0, 2.0]) is None                      # too few points
    assert ev.pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None            # zero variance


@pytest.mark.parametrize("label,target,expected", [
    ("positive", 0.3, True), ("strong positive", -0.1, False), ("negative", -0.2, True),
    ("negative", 0.2, False), ("flat", 0.5, None), ("mixed", 0.5, None), ("positive", 0.0, None),
    ("stable", 0.5, None), (None, 0.5, None), ("positive", None, None),
])
def test_sign_hit_excludes_flat(label, target, expected):
    assert ev.sign_hit(label, target) is expected


def test_score_signals():
    signals = [
        {"name": "gift_gap", "ok": True, "label": "positive", "value": 0.317},
        {"name": "us_futures", "ok": True, "label": "flat", "value": 0.12},
        {"name": "us_cash", "ok": False, "label": None, "value": None},
        {"name": "breadth_nifty", "ok": True, "label": "negative", "value": -0.3},
    ]
    scores = {s["name"]: s for s in ev.score_signals(signals, realized_gap=0.291, day_return=-0.4)}
    assert scores["gift_gap"] == {"name": "gift_gap", "hit_gap": 1, "hit_day": 0, "abs_error": pytest.approx(0.026)}
    assert scores["us_futures"]["hit_gap"] is None          # flat: excluded
    assert scores["us_cash"]["hit_gap"] is None             # failed source: excluded
    assert scores["breadth_nifty"] == {"name": "breadth_nifty", "hit_gap": 0, "hit_day": 1, "abs_error": None}


def seed(store: Store, n: int) -> None:
    """n days: gift_gap is right on 3 of every 4 days; the market is up on 2 of every 4."""
    for i in range(n):
        day = date(2026, 1, 1).toordinal() + i
        day = date.fromordinal(day).isoformat()
        gap = 0.4 if i % 4 in (0, 1) else -0.4
        call = gap if i % 4 != 3 else -gap
        store.save_signals(day, [
            {"name": "gift_gap", "ok": True, "label": "positive" if call > 0 else "negative",
             "value": call * 0.9, "detail": {"method": "basis-adjusted" if i % 2 else "unadjusted"}},
            {"name": "us_cash", "ok": True, "label": "flat", "value": 0.0},
        ])
        store.save_outcome(day, {"realized_gap": gap, "day_return": gap / 2, "nifty_close": 100.0})


def test_report_hit_rate_ci_and_baseline(cfg, tmp_path):
    store = Store(tmp_path / "t.db")
    seed(store, 40)
    report = ev.build_report(store.evaluation_rows(), store.scored_days(), 90, date(2026, 1, 1), cfg)
    gift = report["signals"]["gift_gap"]["gap"]
    assert (gift["n"], gift["hits"], gift["hit_rate"]) == (40, 30, 0.75)
    assert (gift["ci_low"], gift["ci_high"]) == ev.wilson_interval(30, 40)
    assert gift["baseline"] == 0.5                           # always-positive on the same days
    assert report["signals"]["us_cash"]["gap"]["n"] == 0     # flat every day: nothing scored
    by_method = {g["method"]: g for g in report["gap_est"]}
    assert by_method["all"]["n"] == 40 and by_method["basis-adjusted"]["n"] == 20
    assert by_method["all"]["mae"] == pytest.approx((30 * 0.04 + 10 * 0.76) / 40)
    assert -1 <= by_method["all"]["spearman"] <= 1 and by_method["all"]["pearson"] > 0
    text = ev.format_report(report)
    assert "gift_gap         40  75.0" in text and "uncalibrated (40/60 scored days)" in text


def test_weights_are_never_tuned_below_sixty_days(cfg, tmp_path):
    store = Store(tmp_path / "t.db")
    seed(store, 59)
    report = ev.build_report(store.evaluation_rows(), store.scored_days(), 365, date(2026, 1, 1), cfg)
    assert report["weights"] == {"status": "not tuned", "reason": "59 scored days < 60"}
    assert not report["calibrated"]
    seed(store, 60)
    report = ev.build_report(store.evaluation_rows(), store.scored_days(), 365, date(2026, 1, 1), cfg)
    assert report["calibrated"] and report["weights"]["status"].startswith("suggestion only")
    assert report["weights"]["weights"]["gift_gap"] == 1.0   # the only signal with an edge here


def test_report_window_filters_by_date(cfg, tmp_path):
    store = Store(tmp_path / "t.db")
    seed(store, 40)
    rows = store.evaluation_rows(date(2026, 1, 31))
    assert {r["trade_date"] for r in rows} == {date(2026, 1, 31).isoformat()} | {f"2026-02-0{d}" for d in range(1, 10)}


# -- store ---------------------------------------------------------------------

def test_rerun_overwrites_same_day_rows(tmp_path):
    store = Store(tmp_path / "t.db")
    day = "2026-11-13"
    for run_id, label in (("first", "negative"), ("second", "positive")):
        store.save_run(day, "morning", {"run_id": run_id, "started_at": "t", "status": "ok"})
        store.save_raw(day, "morning", "marketStatus", ok=True, payload={"run": run_id})
        store.save_signals(day, [{"name": "gift_gap", "ok": True, "label": label, "value": 0.3,
                                  "flags": ["unadjusted"], "detail": {"k": 1}}])
        store.save_outcome(day, {"realized_gap": 0.2, "gift_last": 1.0, "gift_fresh": True})
    count = lambda table: store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: E731
    assert [count(t) for t in ("runs", "raw_snapshots", "signals", "outcomes")] == [1, 1, 1, 1]
    assert store._db.execute("SELECT run_id FROM runs").fetchone()[0] == "second"
    assert json.loads(store._db.execute("SELECT payload FROM raw_snapshots").fetchone()[0]) == {"run": "second"}
    signal = store.get_signals(day)[0]
    assert signal["label"] == "positive" and signal["flags"] == ["unadjusted"] and signal["detail"] == {"k": 1}
    assert store.get_outcome(day)["gift_fresh"] is True


def test_morning_and_close_runs_coexist_and_scores_reset_on_rerun(tmp_path):
    store = Store(tmp_path / "t.db")
    day = "2026-11-13"
    store.save_run(day, "morning", {"run_id": "a", "started_at": "t", "status": "ok"})
    store.save_run(day, "close", {"run_id": "b", "started_at": "t", "status": "ok"})
    assert store._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    store.save_signals(day, [{"name": "gift_gap", "ok": True, "label": "positive", "value": 0.3}])
    store.save_scores(day, [{"name": "gift_gap", "hit_gap": 1, "hit_day": 0, "abs_error": 0.02}])
    assert store.get_signals(day)[0]["hit_gap"] == 1
    store.save_signals(day, [{"name": "gift_gap", "ok": True, "label": "negative", "value": -0.3}])
    assert store.get_signals(day)[0]["hit_gap"] is None      # stale score cleared with the old signal


def test_latest_outcome_before_and_scored_days(tmp_path):
    store = Store(tmp_path / "t.db")
    store.save_outcome("2026-11-11", {"gift_last": 1.0})
    store.save_outcome("2026-11-12", {"gift_last": 2.0, "realized_gap": 0.1})
    assert store.latest_outcome_before("2026-11-13")["gift_last"] == 2.0
    assert store.latest_outcome_before("2026-11-12")["gift_last"] == 1.0
    assert store.latest_outcome_before("2026-11-11") is None
    assert store.scored_days() == 0                          # outcome without morning signals
    store.save_signals("2026-11-12", [{"name": "us_cash", "ok": True, "label": "flat", "value": 0.0}])
    assert store.scored_days() == 1


def test_append_jsonl(tmp_path):
    path = tmp_path / "data" / "runs.jsonl"
    append_jsonl(path, {"a": 1, "when": date(2026, 11, 13)})
    append_jsonl(path, {"a": 2})
    lines = path.read_text().splitlines()
    assert [json.loads(line)["a"] for line in lines] == [1, 2]
    assert json.loads(lines[0])["when"] == "2026-11-13"
