"""Optional LLM rephrasing of the computed signals. Off unless LLM_PROVIDER is set.

Guard rails:
* the model only ever sees the computed signals JSON;
* one call per trade date (the result, good or bad, is cached in data/llm_state.json);
* 10 s timeout, no retries;
* post-check: every number in the output must appear in the input JSON,
  otherwise the output is discarded and the template brief is used alone.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("preopen_brief.llm")

_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")

SYSTEM_PROMPT = (
    "You rephrase a pre-market data brief for Indian equities. Use ONLY the JSON you are given. "
    "Write at most {n} plain-text sentences. Do not add any number, fact, prediction, opinion or "
    "advice that is not in the JSON. Copy numbers exactly as written; do not round or convert them."
)


def numbers_in(text: str) -> set[Decimal]:
    found = set()
    for token in _NUMBER.findall(text):
        try:
            found.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def passes_number_check(output: str, input_json: str) -> bool:
    """True iff every number in ``output`` also appears (by value) in ``input_json``."""
    return numbers_in(output) <= numbers_in(input_json)


def sentence_count(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def llm_input(signals: dict[str, Any], composite: dict[str, Any]) -> dict[str, Any]:
    """Compact, pre-rounded view of the signals: exactly what the model may talk about."""

    def r(value: Any) -> Any:
        return round(value, 2) if isinstance(value, float) else value

    out: dict[str, Any] = {}
    for name, s in signals.items():
        if not s["ok"]:
            out[name] = {"available": False, "reason": s.get("reason")}
            continue
        entry = {"label": s["label"], "value": r(s["value"]), "flags": s.get("flags") or []}
        for key in ("components", "percentile", "change_pct", "advances", "declines", "unchanged", "method", "last"):
            if key in s["detail"]:
                value = s["detail"][key]
                entry[key] = {k: r(v) for k, v in value.items()} if isinstance(value, dict) else r(value)
        if s["detail"].get("universe"):  # lets the model say "NIFTY 50" without failing the number check
            entry["universe"] = "NIFTY 50" if s["detail"]["universe"] == "NIFTY" else "all NSE stocks"
        out[name] = entry
    out["bias"] = {
        "label": composite["bias"],
        "sources_ok": composite["sources_ok"],
        "sources_total": composite["sources_total"],
        "calibrated": composite["calibrated"],
    }
    return out


def _call_groq(prompt: str, user: str, api_key: str, model: str, client: httpx.Client) -> str:
    response = client.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "temperature": 0,
            "max_tokens": 200,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
        },
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def _call_gemini(prompt: str, user: str, api_key: str, model: str, client: httpx.Client) -> str:
    response = client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={
            "systemInstruction": {"parts": [{"text": prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 200},
        },
    )
    response.raise_for_status()
    return response.json()["candidates"][0]["content"]["parts"][0]["text"]


def summarize(
    payload: dict[str, Any],
    trade_date: str,
    settings: Settings,
    cfg: dict[str, Any],
    state_path: Path | None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Validated summary text, or None (provider off, call failed, or post-check failed)."""
    provider = settings.llm_provider
    key = {"groq": settings.groq_api_key, "gemini": settings.gemini_api_key}.get(provider, "")
    if provider == "none" or not key:
        return None

    if state_path is not None and state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("trade_date") == trade_date:  # already called today
                return state.get("summary")
        except (OSError, ValueError):
            pass

    user = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    max_sentences = int(cfg["llm"]["max_sentences"])
    prompt = SYSTEM_PROMPT.format(n=max_sentences)
    summary: str | None = None
    status = "ok"
    try:
        with httpx.Client(timeout=float(cfg["llm"]["timeout_seconds"]), transport=transport) as client:
            if provider == "groq":
                text = _call_groq(prompt, user, key, cfg["llm"]["groq_model"], client)
            else:
                text = _call_gemini(prompt, user, key, cfg["llm"]["gemini_model"], client)
        text = " ".join(str(text).split())
        if not text:
            status = "discarded: empty output"
        elif not passes_number_check(text, user):
            status = "discarded: output contains numbers not in the input"
        elif sentence_count(text) > max_sentences:
            status = "discarded: too many sentences"
        else:
            summary = text
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        status = f"failed: {type(exc).__name__}"
    log.info("llm summary %s", status, extra={"data": {"provider": provider}})

    if state_path is not None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"trade_date": trade_date, "status": status, "summary": summary}), encoding="utf-8"
        )
    return summary
