# preopen-brief

A small, local, free-tier system that runs every NSE trading day, collects
pre-market cues for Indian equities, computes deterministic signals, sends a
Telegram brief, logs everything, and scores its own past signals against what
the market actually did.

```
Pre-open brief · 2026-11-13 09:09 IST
US (prev close): S&P +0.42% | Nasdaq +0.61% | Dow +0.18% -> positive
US futures: ES +0.12% | NQ +0.20% | YM +0.05% -> flat
GIFT Nifty: 26,030.00 | est. gap +0.32% (basis-adjusted) -> positive
India VIX: 13.42 (prev close, 16th pct, -1.18%) -> stable
Breadth NIFTY50 pre-open: 31 adv / 17 dec / 2 unch (ratio 1.82) -> positive
Breadth ALL pre-open: 1040 adv / 890 dec / 95 unch (ratio 1.17) -> flat
Bias: positive · completeness 6/6 · uncalibrated
Not investment advice.
```
*(Output of the offline demo in Setup step 2, from synthetic fixtures - not real market data.)*

**Informational only. Not investment advice.** Personal use; do not redistribute NSE data.

## Status - read this first

| Part | State |
|---|---|
| Signals, storage, scoring, report, rendering, retries, request cap | Tested offline (`pytest`, 179 tests, Python 3.11 and 3.13) |
| NSE parsers | Written against **synthetic** fixtures. Field names not in the spec are the author's recollection and are marked `ASSUMED` in `preopen_brief/sources/nse.py`. **Run `probe.py` once (step 3) before trusting the brief.** |
| Live NSE / Yahoo / Telegram calls | Never executed by the author (built on a machine with no access to them). Failure handling was exercised for real; success paths only through mocks. |
| Windows Task Scheduler XML, Termux script | Well-formed / syntax-checked, **not run** on Windows or Android. |

## Requirements

- Python 3.11+ on your own machine with an **Indian residential IP**. NSE blocks most
  cloud and foreign IPs, so this is not designed for GitHub Actions / AWS / GCP.
- Free services only: a Telegram bot; optionally healthchecks.io, Groq or Gemini (all free tier, no card).

## Setup

```bash
# 1. install
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
mkdir -p data logs
cp .env.example .env                             # then fill it in (step 2)

# 2. see the whole thing work offline: no network, no secrets, synthetic data
#    (Thursday's close snapshot, then Friday's brief, close and Saturday's report)
DEMO="--dry-run --data-dir /tmp/preopen-demo"
.venv/bin/python -m preopen_brief.jobs.close   $DEMO --offline tests/fixtures/close_prev --now 2026-11-12T15:32:10
.venv/bin/python -m preopen_brief.jobs.morning $DEMO --offline tests/fixtures/morning    --now 2026-11-13T09:09:05
.venv/bin/python -m preopen_brief.jobs.close   $DEMO --offline tests/fixtures/close      --now 2026-11-13T15:32:10
.venv/bin/python -m preopen_brief.jobs.weekly  $DEMO --offline tests/fixtures/close      --now 2026-11-14T10:00:00

# 3. probe the real endpoints (Indian IP; best around 09:08 IST on a trading day)
.venv/bin/python probe.py

# 4. real data, printed instead of sent
.venv/bin/python -m preopen_brief.jobs.morning --dry-run
```

### Step 3 matters: `probe.py`

NSE's JSON is unofficial and undocumented, so nothing here assumes it blindly:

- `python probe.py` fetches each endpoint **once** (6 NSE requests, >= 450 ms apart, plus
  Yahoo), saves the raw responses to `fixtures/<endpoint>_<timestamp>.json`, prints each
  key tree, and then checks every field the parsers need.
- `python probe.py --validate` re-runs just the check on the newest saved fixtures (no network).

Each line is `ok` or `MISSING`. For a `MISSING` field, find the real key in the printed
tree and add it to the matching entry of `ALIASES` at the top of
`preopen_brief/sources/nse.py` - lookups are case-insensitive and try each alias in
order, so that is normally a one-word fix. Run it once before the open (~09:08) and once
after the close (~15:35): the pre-open and index payloads differ between those times.
A parser that cannot find a field never guesses - that line of the brief shows
`n/a (<field> missing)`.

Things only a real response can confirm (and the code is defensive about):
`TIMESTMP` format and time zone (assumed IST), whether `allIndices` before 09:15 still
shows yesterday's session (the code decides from the payload's own date), and whether
NSE's GIFT quote is fresh enough for the 15-minute staleness limit
(`signals.gift_gap.max_quote_age_minutes`).

## Telegram setup

1. In Telegram, open a chat with **@BotFather** -> `/newbot` -> pick a name and a username
   ending in `bot`. BotFather replies with a token like `123456789:AAH...`. Put it in
   `.env` as `TELEGRAM_BOT_TOKEN`.
2. **Send your new bot a message first** (open its chat, press Start, type anything).
   Bots cannot message you until you do, and `getUpdates` is empty until then.
3. Get your chat id:
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```
   Find `"chat":{"id":123456789,...}` in the reply and put that number in `.env` as
   `TELEGRAM_CHAT_ID`. (For a group: add the bot, send a message in the group, and use
   the group's negative id.) If `result` is `[]`, send the bot another message and retry.
4. Test: `.venv/bin/python -m preopen_brief.jobs.weekly` sends a (mostly empty) report.

The token is a password: it lives only in `.env` (git-ignored) and is scrubbed from all logs.

## Scheduling (Asia/Kolkata)

| When | Job | What it does |
|---|---|---|
| 09:09 Mon-Fri | `python -m preopen_brief.jobs.morning` | Fetch cues, compute signals, send the brief |
| 15:32 Mon-Fri | `python -m preopen_brief.jobs.close` | Snapshot GIFT + NIFTY close, score the morning's signals |
| Sat 10:00 | `python -m preopen_brief.jobs.weekly` | Evaluation report to Telegram |

The morning and close jobs first check that today is a trading day - Mon-Fri and not in
NSE's trading-holiday list (CM segment, cached in `data/holidays_<year>.json`, refreshed
every 30 days) - and exit quietly otherwise. `marketStatus` is fetched too and its trade
date is logged as a cross-check, but it does not decide: at 09:09 the cash market is not
"open" yet. If the holiday list cannot be fetched and there is no cache, weekdays are
assumed open. `--force` skips the check.

**Linux / macOS (cron):** edit the path in [`scheduling/crontab.txt`](scheduling/crontab.txt)
and paste it into `crontab -e`. It sets `CRON_TZ=Asia/Kolkata`; cronie honours that, but
Debian/Ubuntu's default cron and macOS cron may ignore it and use the system time zone -
on those, keep the system time zone on IST. The machine must be awake at those times.

**Windows (Task Scheduler):** three task files in [`scheduling/windows/`](scheduling/windows).
Fix the `C:\preopen-brief` paths inside, then in PowerShell:
```powershell
Register-ScheduledTask -TaskName "preopen-brief morning" -Xml (Get-Content -Raw scheduling\windows\preopen-brief-morning.xml)
Register-ScheduledTask -TaskName "preopen-brief close"   -Xml (Get-Content -Raw scheduling\windows\preopen-brief-close.xml)
Register-ScheduledTask -TaskName "preopen-brief weekly"  -Xml (Get-Content -Raw scheduling\windows\preopen-brief-weekly.xml)
```
Triggers use the PC's local time, so Windows must be set to (UTC+05:30). The tasks wake
the PC from sleep (not from shutdown) and are not run late if missed, because a
pre-open brief at 11:00 is worthless and a late close snapshot would corrupt the basis.

**Android (Termux):** `bash scheduling/termux-setup.sh` installs `cronie` and
`termux-services`, enables `crond`, takes `termux-wake-lock`, and installs the crontab.
Read the caveats at the top of that script (pandas / curl_cffi on Android; battery optimisation).

## What the signals mean

All thresholds and weights are in [`config.yaml`](config.yaml); signal functions are pure
(`preopen_brief/signals.py`, `composite.py`).

| Line | Definition | Labels |
|---|---|---|
| **US (prev close)** | Mean % change of S&P 500, Nasdaq, Dow in the last completed US session. Marked stale (and excluded) if an Indian session has already traded after it, e.g. after a US holiday. | +-0.3 % band: positive / negative / flat |
| **US futures** | Mean % change of ES, NQ, YM from the price at the last US cash close (16:00 New York, ~settlement) to now. | same band |
| **GIFT Nifty** | `gap_est = (GIFT_now - GIFT_at_yesterday's_15:32_snapshot) / NIFTY_prev_close` - "basis-adjusted", because the futures premium cancels out. Falls back to GIFT's own % change, shown as "unadjusted", when there is no usable snapshot from the previous session or the contract rolled (`EXPIRYDATE` changed). A quote older than 15 minutes is rejected. | < 0.25 % flat, >= 0.75 % strong |
| **India VIX** | Level, day change, percentile vs the trailing 252 sessions. "prev close" when fetched before 09:15. | < 12 calm, 12-15 stable, 15-20 elevated, > 20 high |
| **Breadth** | `(adv - dec) / (adv + dec + unch)` from the pre-open book, for NIFTY 50 and for all stocks; ratio = `adv / max(dec, 1)`. "indicative" if fetched before 09:08. | +-0.2 band |
| **Bias** | Weighted sum of US cash, US futures, GIFT gap and breadth (weights renormalised over what is available) -> positive / negative / mixed. `completeness k/6` = lines with data. "uncalibrated" until 60 days have been scored. | |

Two deliberate rules: **VIX never feeds the bias** (it measures how much, not which way; it
only adds a "lower confidence" note when elevated or high), and the raw GIFT-minus-NIFTY
spread is stored as a diagnostic but never used for the label (it contains the basis).

A source that fails shows `n/a (<reason>)` on its own line; lines are never dropped.
An unhandled crash sends a one-line alert with the `run_id` to look up in `logs/preopen.log`.

## Reading the evaluation report

`python report.py --days 90` (or wait for Saturday's Telegram message):

```
Sign hit vs opening gap (flat/mixed excluded)
signal            n  hit%  95% CI        base%   edge
gift_gap         40  75.0  [59.8, 85.8]   50.0  +25.0
```

- **n** - days on which the signal made a directional call *and* an outcome exists.
  Days labelled flat (or bias "mixed") are excluded, so n differs per signal.
- **hit%** - share of those days where the call matched the sign of the opening gap
  (`open / prev_close - 1`).
- **95% CI** - Wilson interval for the hit rate. This is the honest part: with n = 20 the
  interval is roughly +-20 points. **If the interval contains 50 %, you cannot tell the
  signal from a coin flip yet.**
- **base%** - what "always say positive" would have scored on the same days. If the
  market gapped up on 60 % of them, a 55 % hit rate is worse than doing nothing.
- **edge** - hit% minus base%. Only an edge whose CI clears the baseline means anything.
- The second table repeats this against the close-to-close **day return** - usually
  weaker, since pre-open cues say more about the open than about the day.
- **gap_est** - for the GIFT estimate: MAE in percentage points (0.15 = typically off by
  0.15 pp), and Pearson / Spearman correlation with the realized gap, split by
  basis-adjusted vs unadjusted so you can see whether the adjustment earns its keep.
- **Weights** - `not tuned` below 60 scored days, always. Above that the report prints a
  *suggestion* derived from each signal's edge; nothing ever rewrites `config.yaml` for you.

Expect weeks of "uncalibrated". Sixty trading days is about three months.

## Storage and logs

- `data/preopen.db` (SQLite): `runs`, `raw_snapshots` (the JSON each source returned, with
  the pre-open order-book detail trimmed), `signals` (plus `hit_gap`, `hit_day`,
  `abs_error` once scored), `outcomes`. `trade_date` is the idempotency key: re-running a
  job overwrites that day's rows.
- `data/runs.jsonl`: one appended line per run.
- `logs/preopen.log`: JSON lines, rotated at 1 MB x 5. Secrets are redacted.

## Optional extras (all off by default)

- **LLM summary** - set `LLM_PROVIDER=groq` or `gemini` plus the key in `.env`. One call
  per day, 10 s timeout. The model sees only the computed signals JSON and is told to
  rephrase in <= 3 sentences. Afterwards every number in its answer must appear in that
  JSON, or the answer is thrown away and the plain template is sent. Model names are in
  `config.yaml` (`llm.groq_model`, `llm.gemini_model`) - free-tier model names change; update them if a call 404s.
- **Asia/FX/crude context line** - `yahoo.context_enabled: true`.
- **Dead-man's switch** - create a check at healthchecks.io and put its ping URL in
  `.env`. Pinged after every successful run (including quiet holiday exits), `/fail` on errors.
- **Close note** - `close.notify: true` sends the day's gap/return and hit/miss per signal.

## Caveats

- **NSE endpoints are unofficial** and change or get blocked without notice. Expect
  occasional `n/a` lines; re-run `probe.py` when one persists. The client is deliberately
  polite: at most 10 requests per run (a normal morning uses 5-6), >= 450 ms apart, one
  cookie refresh on 401/403 and then it stops asking.
- If NSE returns 403 even from a home connection, it may be rejecting the TLS fingerprint
  rather than the IP. `nse.backend: curl_cffi` in `config.yaml` switches to a
  browser-like client (experimental, untested against NSE).
- **yfinance is unofficial and rate-limited.** It can return nothing for a ticker or for
  everything; those lines then show `n/a`. Yahoo index-futures data is delayed ~10 minutes.
- "US futures vs settlement" is approximated by the 16:00 New York price from hourly
  bars, not the official CME settlement. US exchange holidays are not modelled.
- GIFT Nifty's quote comes from NSE's `marketStatus`; its freshness there is unverified.
- This reports cues and how they scored historically. It predicts nothing.

## Design notes (where this differs from a literal reading of the spec)

- **Two Yahoo calls, not one.** Daily bars cannot express "futures vs prior settlement"
  (Yahoo's daily futures bars do not line up with the settlement), so the default makes a second
  batched call for hourly bars of ES/NQ/YM. `yahoo.futures_reference: daily` restores a
  single call, with the futures line flagged "daily-bar ref".
- **An extra "Breadth ALL" line** so both universes are visible; the bias uses NIFTY
  (`composite.breadth_universe`).
- **Scoring uses two targets**, the opening gap (primary) and the day return.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

No network (sockets are blocked in the suite). Covers parsers and signals against the
fixtures, plus: holiday / weekend, partial data, NSE 403, stale GIFT timestamp, contract
roll, zero declines, missing yfinance ticker, US DST changes, request cap and spacing,
idempotent re-runs, secret redaction, and the LLM number guard.
`tests/fixtures/` is synthetic; regenerate with `python tests/make_synthetic_fixtures.py`.

## Layout

```
preopen_brief/
  sources/nse.py, sources/yahoo.py     fetching + parsing
  signals.py, composite.py             pure signal functions
  evaluate.py, store.py                scoring, statistics, SQLite
  notify.py, llm.py, config.py         Telegram, optional LLM, config + logging
  tradingday.py, retry.py              holiday calendar, shared primitives
  jobs/morning.py, close.py, weekly.py entry points (jobs/common.py: shared plumbing)
probe.py  report.py  config.yaml  .env.example  requirements.txt
scheduling/   crontab.txt, windows/*.xml, termux-setup.sh
tests/        pytest suite + synthetic fixtures
```
