"""SQLite storage. ``trade_date`` is the idempotency key: re-runs overwrite same-day rows."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    trade_date  TEXT NOT NULL,
    job         TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    sources_ok  INTEGER,
    sources_total INTEGER,
    detail      TEXT,
    PRIMARY KEY (trade_date, job)
);
CREATE TABLE IF NOT EXISTS raw_snapshots (
    trade_date TEXT NOT NULL,
    job        TEXT NOT NULL,
    source     TEXT NOT NULL,
    fetched_at TEXT,
    ok         INTEGER NOT NULL,
    error      TEXT,
    payload    TEXT,
    PRIMARY KEY (trade_date, job, source)
);
CREATE TABLE IF NOT EXISTS signals (
    trade_date TEXT NOT NULL,
    name       TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    label      TEXT,
    value      REAL,
    reason     TEXT,
    flags      TEXT,
    detail     TEXT,
    hit_gap    INTEGER,
    hit_day    INTEGER,
    abs_error  REAL,
    PRIMARY KEY (trade_date, name)
);
CREATE TABLE IF NOT EXISTS outcomes (
    trade_date       TEXT PRIMARY KEY,
    nifty_open       REAL,
    nifty_close      REAL,
    nifty_prev_close REAL,
    nifty_source     TEXT,
    realized_gap     REAL,
    day_return       REAL,
    intraday_return  REAL,
    gift_last        REAL,
    gift_expiry      TEXT,
    gift_timestamp   TEXT,
    gift_fresh       INTEGER,
    recorded_at      TEXT
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, default=_default, ensure_ascii=False)


def _default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _day(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else value


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- runs / raw ----------------------------------------------------------

    def save_run(self, trade_date: date | str, job: str, run: dict[str, Any]) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    _day(trade_date),
                    job,
                    run["run_id"],
                    run["started_at"],
                    run.get("finished_at"),
                    run["status"],
                    run.get("sources_ok"),
                    run.get("sources_total"),
                    _json(run.get("detail") or {}),
                ),
            )

    def save_raw(
        self,
        trade_date: date | str,
        job: str,
        source: str,
        *,
        ok: bool,
        payload: Any,
        error: str | None = None,
        fetched_at: datetime | None = None,
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO raw_snapshots VALUES (?,?,?,?,?,?,?)",
                (
                    _day(trade_date),
                    job,
                    source,
                    fetched_at.isoformat() if fetched_at else None,
                    int(ok),
                    error,
                    _json(payload) if payload is not None else None,
                ),
            )

    # -- signals -------------------------------------------------------------

    def save_signals(self, trade_date: date | str, signals: list[dict[str, Any]]) -> None:
        """Replace all of a day's signals (and clear any earlier scoring)."""
        day = _day(trade_date)
        with self._db:
            self._db.execute("DELETE FROM signals WHERE trade_date = ?", (day,))
            self._db.executemany(
                "INSERT INTO signals (trade_date, name, ok, label, value, reason, flags, detail)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        day,
                        s["name"],
                        int(bool(s["ok"])),
                        s.get("label"),
                        s.get("value"),
                        s.get("reason"),
                        _json(s.get("flags") or []),
                        _json(s.get("detail") or {}),
                    )
                    for s in signals
                ],
            )

    def get_signals(self, trade_date: date | str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM signals WHERE trade_date = ? ORDER BY name", (_day(trade_date),)
        ).fetchall()
        return [self._signal_row(r) for r in rows]

    @staticmethod
    def _signal_row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["ok"] = bool(out["ok"])
        out["flags"] = json.loads(out["flags"] or "[]")
        out["detail"] = json.loads(out["detail"] or "{}")
        return out

    def save_scores(self, trade_date: date | str, scores: list[dict[str, Any]]) -> None:
        with self._db:
            self._db.executemany(
                "UPDATE signals SET hit_gap = ?, hit_day = ?, abs_error = ? WHERE trade_date = ? AND name = ?",
                [(s.get("hit_gap"), s.get("hit_day"), s.get("abs_error"), _day(trade_date), s["name"]) for s in scores],
            )

    # -- outcomes ------------------------------------------------------------

    def save_outcome(self, trade_date: date | str, outcome: dict[str, Any]) -> None:
        columns = (
            "nifty_open", "nifty_close", "nifty_prev_close", "nifty_source", "realized_gap",
            "day_return", "intraday_return", "gift_last", "gift_expiry", "gift_timestamp",
            "gift_fresh", "recorded_at",
        )
        values = [outcome.get(c) for c in columns]
        values[columns.index("gift_fresh")] = (
            None if outcome.get("gift_fresh") is None else int(bool(outcome["gift_fresh"]))
        )
        with self._db:
            self._db.execute(
                f"INSERT OR REPLACE INTO outcomes (trade_date, {', '.join(columns)})"
                f" VALUES (?{', ?' * len(columns)})",
                [_day(trade_date), *values],
            )

    def get_outcome(self, trade_date: date | str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM outcomes WHERE trade_date = ?", (_day(trade_date),)).fetchone()
        return self._outcome_row(row)

    def latest_outcome_before(self, trade_date: date | str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM outcomes WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1", (_day(trade_date),)
        ).fetchone()
        return self._outcome_row(row)

    @staticmethod
    def _outcome_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        out["gift_fresh"] = None if out["gift_fresh"] is None else bool(out["gift_fresh"])
        return out

    # -- evaluation ----------------------------------------------------------

    def scored_days(self) -> int:
        """Days that have both morning signals and a realized gap."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM outcomes o WHERE o.realized_gap IS NOT NULL"
            " AND EXISTS (SELECT 1 FROM signals s WHERE s.trade_date = o.trade_date)"
        ).fetchone()
        return int(row[0])

    def evaluation_rows(self, since: date | str | None = None) -> list[dict[str, Any]]:
        """One row per (day, signal) for days with a realized outcome."""
        query = (
            "SELECT s.trade_date, s.name, s.ok, s.label, s.value, s.flags, s.detail,"
            " o.realized_gap, o.day_return"
            " FROM signals s JOIN outcomes o ON o.trade_date = s.trade_date"
            " WHERE o.realized_gap IS NOT NULL"
        )
        params: tuple[Any, ...] = ()
        if since is not None:
            query += " AND s.trade_date >= ?"
            params = (_day(since),)
        rows = self._db.execute(query + " ORDER BY s.trade_date, s.name", params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["ok"] = bool(item["ok"])
            item["flags"] = json.loads(item["flags"] or "[]")
            item["detail"] = json.loads(item["detail"] or "{}")
            out.append(item)
        return out


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_json(record) + "\n")
