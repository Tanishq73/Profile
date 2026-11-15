"""probe.py - look before you parse.

Fetches every endpoint exactly once, saves the raw response to
``fixtures/<endpoint>_<timestamp>.json``, prints its key tree, and then checks
the saved responses against what the parsers in preopen_brief need.

    python probe.py                  # 6 NSE requests + Yahoo, save, print, validate
    python probe.py --no-yahoo       # NSE only
    python probe.py --validate       # no network: re-check the newest saved fixtures
    python probe.py --validate DIR   # ... in another directory

Run it from an Indian residential IP (NSE blocks most cloud / foreign IPs),
ideally once around 09:08 IST and once after 15:30 IST: the pre-open and
allIndices payloads differ between those times. Fixtures are for your own
debugging only; do not redistribute NSE data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from preopen_brief.config import load_config, now_ist, resolve_path
from preopen_brief.retry import SourceError
from preopen_brief.sources import nse, yahoo

MAX_DEPTH = 7
MAX_KEYS = 60


def key_tree(value: Any, indent: int = 0, depth: int = 0) -> list[str]:
    """Indented description of a JSON value: keys, types, list lengths, sample scalars."""
    pad = "  " * indent
    if depth >= MAX_DEPTH:
        return [f"{pad}..."]
    lines: list[str] = []
    if isinstance(value, dict):
        for i, (key, item) in enumerate(value.items()):
            if i >= MAX_KEYS:
                lines.append(f"{pad}... {len(value) - MAX_KEYS} more keys")
                break
            if isinstance(item, dict):
                lines.append(f"{pad}{key}: object ({len(item)} keys)")
                lines += key_tree(item, indent + 1, depth + 1)
            elif isinstance(item, list):
                kinds = sorted({type(x).__name__ for x in item}) or ["empty"]
                lines.append(f"{pad}{key}: list[{len(item)}] of {'/'.join(kinds)}")
                if item and isinstance(item[0], (dict, list)):
                    lines.append(f"{pad}  [0]:")
                    lines += key_tree(item[0], indent + 2, depth + 1)
            else:
                lines.append(f"{pad}{key}: {type(item).__name__} = {json.dumps(item, ensure_ascii=False)[:70]}")
    elif isinstance(value, list):
        lines.append(f"{pad}list[{len(value)}]")
        if value:
            lines += key_tree(value[0], indent + 1, depth + 1)
    else:
        lines.append(f"{pad}{type(value).__name__} = {json.dumps(value, ensure_ascii=False)[:70]}")
    return lines


def save(directory: Path, name: str, stamp: str, data: Any) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}_{stamp}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    return path


def validate_yahoo(payload: dict[str, Any], cfg: dict[str, Any]) -> list[tuple[str, bool, str]]:
    checks = []
    for kind, error in (payload.get("errors") or {}).items():
        checks.append((f"{kind} call", False, error))
    for ticker in yahoo.all_daily_tickers(cfg):
        bars = yahoo.bars_for(payload, ticker)
        need = int(cfg["signals"]["vix"]["min_history"]) if ticker == cfg["yahoo"]["india_vix"] else 2
        detail = f"{len(bars)} daily bars" + (f", last {bars[-1]['t']} close {bars[-1]['c']}" if bars else "")
        checks.append((f"daily {ticker}", len(bars) >= need, detail))
    if cfg["yahoo"]["futures_reference"] == "cash_close":
        for ticker in cfg["yahoo"]["us_futures"]:
            bars = yahoo.bars_for(payload, ticker, "intraday")
            detail = f"{len(bars)} intraday bars" + (f", last {bars[-1]['t']}" if bars else "")
            checks.append((f"intraday {ticker}", len(bars) >= 2, detail))
    return checks


def report(name: str, checks: list[tuple[str, bool, str]]) -> int:
    """Print a validation table; return the number of failed checks."""
    bad = 0
    print(f"\n-- parser check: {name}")
    for field, ok, detail in checks:
        bad += 0 if ok else 1
        print(f"   {'ok     ' if ok else 'MISSING'} {field:<34} {detail}")
    return bad


def validate_directory(directory: Path, cfg: dict[str, Any]) -> int:
    bad = seen = 0
    for name in nse.ENDPOINTS:
        matches = sorted(directory.glob(f"{name}_*.json"))
        if not matches:
            print(f"\n-- parser check: {name}: no fixture in {directory}")
            continue
        seen += 1
        with open(matches[-1], encoding="utf-8") as fh:
            bad += report(f"{name} ({matches[-1].name})", nse.validate(name, json.load(fh), cfg["nse"]["holiday_segment"]))
    matches = sorted(directory.glob("yahoo_*.json"))
    if matches:
        seen += 1
        with open(matches[-1], encoding="utf-8") as fh:
            bad += report(f"yahoo ({matches[-1].name})", validate_yahoo(json.load(fh), cfg))
    if not seen:
        print(f"No fixtures found in {directory}.")
        return 1
    print(f"\n{'All parser checks passed.' if not bad else f'{bad} field(s) the parsers need were not found - see MISSING above.'}")
    if bad:
        print("Fix: add the real key name to ALIASES in preopen_brief/sources/nse.py (or share the key tree above).")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--validate", nargs="?", const="", metavar="DIR", help="no network; check saved fixtures against the parsers")
    parser.add_argument("--no-yahoo", action="store_true")
    parser.add_argument("--no-nse", action="store_true")
    parser.add_argument("--out", metavar="DIR", help="where to save fixtures (default: paths.fixtures_dir)")
    parser.add_argument("--config", metavar="FILE")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    directory = resolve_path(cfg, "fixtures_dir", args.out)
    if args.validate is not None:
        return validate_directory(Path(args.validate) if args.validate else directory, cfg)

    stamp = now_ist().strftime("%Y%m%d_%H%M%S")
    failures = 0
    if not args.no_nse:
        client = nse.NseClient(cfg)
        try:
            for name, path in nse.ENDPOINTS.items():
                print(f"\n=== {name}  GET {path}")
                try:
                    data = client.fetch(name)
                except SourceError as exc:
                    failures += 1
                    print(f"   FAILED: {exc}")
                    continue
                print(f"   saved {save(directory, name, stamp, data)}")
                print("\n".join("   " + line for line in key_tree(data)))
        finally:
            client.close()
        print(f"\nNSE requests made: {client.requests_made} (cap {cfg['nse']['max_requests_per_run']})")
    if not args.no_yahoo:
        print("\n=== yahoo  yfinance batched download")
        try:
            payload = yahoo.YahooSource(cfg).fetch()
            print(f"   saved {save(directory, 'yahoo', stamp, payload)}")
            for kind in ("daily", "intraday"):
                for ticker, bars in payload[kind].items():
                    sample = f"first {bars[0]} last {bars[-1]}" if bars else "NO DATA"
                    print(f"   {kind:<8} {ticker:<10} {len(bars):>4} bars  {sample}")
        except SourceError as exc:
            failures += 1
            print(f"   FAILED: {exc}")

    code = validate_directory(directory, cfg)
    if failures:
        print(f"{failures} source(s) could not be fetched.")
    return 1 if failures or code else 0


if __name__ == "__main__":
    sys.exit(main())
