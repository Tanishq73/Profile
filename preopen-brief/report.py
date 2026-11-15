"""Evaluation report on the command line.

    python report.py --days 90          # fixed-width text
    python report.py --days 90 --json   # machine-readable

Read-only: no network, nothing sent, no weights changed.
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta

from preopen_brief import DISCLAIMER
from preopen_brief.config import load_config, now_ist, resolve_path
from preopen_brief.evaluate import build_report, format_report
from preopen_brief.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="preopen-brief evaluation report")
    parser.add_argument("--days", type=int, help="look-back window in calendar days")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--data-dir", help="override paths.data_dir")
    parser.add_argument("--config", help="path to config.yaml")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    days = args.days or int(cfg["evaluation"]["default_days"])
    db = resolve_path(cfg, "data_dir", args.data_dir) / "preopen.db"
    if not db.exists():
        print(f"No database at {db} yet - run the morning and close jobs first.")
        return 1
    store = Store(db)
    try:
        since = now_ist().date() - timedelta(days=days)
        report = build_report(store.evaluation_rows(since), store.scored_days(), days, since, cfg)
    finally:
        store.close()
    print(json.dumps(report, indent=2) if args.json else f"{format_report(report)}\n{DISCLAIMER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
