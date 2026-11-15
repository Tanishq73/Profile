"""Brief rendering and delivery (Telegram, healthchecks.io).

Rendering is pure and produces plain-text lines; ``to_html`` escapes them for
Telegram's HTML parse mode. A failed source always renders as
``n/a (<reason>)`` on its own line - it is never dropped.
"""

from __future__ import annotations

import html
import logging
import random
import time
from datetime import datetime
from typing import Any, Callable

import httpx

from . import DISCLAIMER
from .composite import Composite
from .config import Settings
from .retry import SourceError, backoff_delay
from .signals import Signal

log = logging.getLogger("preopen_brief.notify")

TELEGRAM_LIMIT = 4096


# --------------------------------------------------------------------------- formatting


def fmt_pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}%"


def fmt_level(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _na(signal: Signal | None) -> str:
    reason = (signal.reason if signal is not None else None) or "not computed"
    return f"n/a ({reason})"


def _components_line(signal: Signal | None) -> str:
    if signal is None or not signal.ok:
        return _na(signal)
    parts = " | ".join(f"{name} {fmt_pct(pct)}" for name, pct in signal.detail["components"].items())
    notes = [f for f in signal.flags if not f.startswith("partial")]
    suffix = f" ({', '.join(notes)})" if notes else ""
    return f"{parts} -> {signal.label}{suffix}"


def _gift_line(signal: Signal | None) -> str:
    if signal is None or not signal.ok:
        return _na(signal)
    method = signal.detail.get("method", "unadjusted")
    return f"{fmt_level(signal.detail['last'])} | est. gap {fmt_pct(signal.value)} ({method}) -> {signal.label}"


def _vix_line(signal: Signal | None) -> str:
    if signal is None or not signal.ok:
        return _na(signal)
    percentile = signal.detail.get("percentile")
    notes = list(signal.flags)
    notes.append(f"{ordinal(int(round(percentile)))} pct" if percentile is not None else "pct n/a")
    notes.append(fmt_pct(signal.detail.get("change_pct")))
    return f"{fmt_level(signal.value)} ({', '.join(notes)}) -> {signal.label}"


def _breadth_line(signal: Signal | None) -> str:
    if signal is None or not signal.ok:
        return _na(signal)
    d = signal.detail
    notes = [f"ratio {d['ad_ratio']:.2f}"] + list(signal.flags)
    return f"{d['advances']} adv / {d['declines']} dec / {d['unchanged']} unch ({', '.join(notes)}) -> {signal.label}"


def brief_lines(
    now: datetime,
    signals: dict[str, Signal],
    composite: Composite,
    *,
    context: dict[str, float | None] | None = None,
    summary: str | None = None,
) -> list[str]:
    """The morning brief as plain-text lines (first line is the title)."""
    lines = [
        f"Pre-open brief · {now:%Y-%m-%d} {now:%H:%M} IST",
        f"US (prev close): {_components_line(signals.get('us_cash'))}",
        f"US futures: {_components_line(signals.get('us_futures'))}",
        f"GIFT Nifty: {_gift_line(signals.get('gift_gap'))}",
        f"India VIX: {_vix_line(signals.get('vix'))}",
        f"Breadth NIFTY50 pre-open: {_breadth_line(signals.get('breadth_nifty'))}",
        f"Breadth ALL pre-open: {_breadth_line(signals.get('breadth_all'))}",
    ]
    if context is not None:
        lines.append("Context: " + " | ".join(f"{name} {fmt_pct(pct)}" for name, pct in context.items()))
    bias = f"Bias: {composite.bias} · completeness {composite.sources_ok}/{composite.sources_total}"
    if not composite.calibrated:
        bias += " · uncalibrated"
    if composite.confidence_note:
        bias += f" · {composite.confidence_note}"
    lines.append(bias)
    if summary:
        lines.append(f"Summary: {summary}")
    lines.append(DISCLAIMER)
    return lines


def to_html(lines: list[str]) -> str:
    """Escape for Telegram parse_mode=HTML; the title line is bold."""
    escaped = [html.escape(line, quote=False) for line in lines]
    return "\n".join([f"<b>{escaped[0]}</b>", *escaped[1:]])


def preformatted_html(title: str, body: str) -> str:
    """Title + monospace body + disclaimer, trimmed to Telegram's message limit."""
    head = f"<b>{html.escape(title, quote=False)}</b>\n<pre>"
    tail = f"</pre>\n{html.escape(DISCLAIMER)}"
    escaped = html.escape(body, quote=False)
    room = TELEGRAM_LIMIT - len(head) - len(tail)
    if len(escaped) > room:
        escaped = escaped[: room - 4].rsplit("\n", 1)[0] + "\n..."
    return head + escaped + tail


def alert_html(job: str, run_id: str, error: str) -> str:
    return html.escape(f"preopen-brief {job} job failed · run_id {run_id} · {error[:300]}", quote=False)


def close_lines(trade_date: Any, outcome: dict[str, Any], scores: list[dict[str, Any]]) -> list[str]:
    hits = [s for s in scores if s["hit_gap"] is not None]
    summary = ", ".join(f"{s['name']} {'hit' if s['hit_gap'] else 'miss'}" for s in hits) or "nothing to score"
    return [
        f"Close · {trade_date}",
        f"NIFTY 50: gap {fmt_pct(outcome.get('realized_gap'))} | day {fmt_pct(outcome.get('day_return'))}",
        f"Morning signals vs gap: {summary}",
        DISCLAIMER,
    ]


# --------------------------------------------------------------------------- delivery


def send_telegram(
    text_html: str,
    settings: Settings,
    cfg: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> None:
    """sendMessage with parse_mode=HTML. Raises SourceError (token-free) on failure."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise SourceError("telegram: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    http = cfg["http"]
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text_html[:TELEGRAM_LIMIT],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    timeout = httpx.Timeout(float(http["read_timeout"]), connect=float(http["connect_timeout"]))
    error = "unknown"
    with httpx.Client(timeout=timeout, transport=transport) as client:
        for attempt in range(1, int(http["max_retries"]) + 2):
            try:
                response = client.post(url, json=payload)
            except httpx.HTTPError as exc:
                error = f"network error ({type(exc).__name__})"
            else:
                if response.status_code == 200:
                    return
                try:
                    description = str(response.json().get("description", ""))[:120]
                except ValueError:
                    description = ""
                error = f"HTTP {response.status_code} {description}".strip()
                if response.status_code != 429 and response.status_code < 500:
                    break
            if attempt <= int(http["max_retries"]):
                sleep(backoff_delay(attempt, float(http["backoff_base"]), float(http["backoff_max"]), rng))
    raise SourceError(f"telegram: {error}")


def ping_healthcheck(url: str, cfg: dict[str, Any], *, ok: bool = True, transport: httpx.BaseTransport | None = None) -> bool:
    """Dead-man's-switch ping (healthchecks.io). Best effort; never raises."""
    if not url:
        return False
    target = url.rstrip("/") + ("" if ok else "/fail")
    http = cfg["http"]
    timeout = httpx.Timeout(float(http["read_timeout"]), connect=float(http["connect_timeout"]))
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            return client.get(target).status_code < 400
    except httpx.HTTPError as exc:
        log.warning("healthcheck ping failed: %s", type(exc).__name__)
        return False
