"""Brief rendering, Telegram delivery, secret redaction and the LLM number guard."""

import json
import logging

import httpx
import pytest

from preopen_brief import llm
from preopen_brief.composite import Composite
from preopen_brief.config import Settings, load_settings, setup_logging
from preopen_brief.notify import (  # imported before the autouse fixture patches the module
    alert_html, brief_lines, ordinal, ping_healthcheck, preformatted_html, send_telegram, to_html,
)
from preopen_brief.retry import SourceError
from preopen_brief.signals import Signal, failed

from .conftest import MORNING_NOW

SETTINGS = Settings(telegram_bot_token="123456:SECRET-TOKEN", telegram_chat_id="42",
                    llm_provider="groq", groq_api_key="gsk_secret")


def good_signals() -> dict[str, Signal]:
    return {
        "us_cash": Signal("us_cash", True, "positive", 0.403, detail={"components": {"S&P": 0.42, "Nasdaq": 0.61, "Dow": 0.18}}),
        "us_futures": Signal("us_futures", True, "flat", 0.123, detail={"components": {"ES": 0.12, "NQ": 0.2, "YM": 0.05}}),
        "gift_gap": Signal("gift_gap", True, "positive", 0.317, flags=["basis-adjusted"],
                           detail={"last": 26030.0, "method": "basis-adjusted"}),
        "vix": Signal("vix", True, "stable", 13.42, flags=["prev close"], detail={"percentile": 34.6, "change_pct": -1.18}),
        "breadth_nifty": Signal("breadth_nifty", True, "positive", 0.28,
                                detail={"advances": 31, "declines": 17, "unchanged": 2, "ad_ratio": 31 / 17}),
        "breadth_all": Signal("breadth_all", True, "flat", 0.074,
                              detail={"advances": 1040, "declines": 890, "unchanged": 95, "ad_ratio": 1040 / 890}),
    }


def composite(**kw) -> Composite:
    base = dict(bias="positive", score=0.6, sources_ok=6, sources_total=6, calibrated=False, scored_days=0)
    return Composite(**{**base, **kw})


def test_brief_matches_template():
    assert brief_lines(MORNING_NOW, good_signals(), composite()) == [
        "Pre-open brief · 2026-11-13 09:09 IST",
        "US (prev close): S&P +0.42% | Nasdaq +0.61% | Dow +0.18% -> positive",
        "US futures: ES +0.12% | NQ +0.20% | YM +0.05% -> flat",
        "GIFT Nifty: 26,030.00 | est. gap +0.32% (basis-adjusted) -> positive",
        "India VIX: 13.42 (prev close, 35th pct, -1.18%) -> stable",
        "Breadth NIFTY50 pre-open: 31 adv / 17 dec / 2 unch (ratio 1.82) -> positive",
        "Breadth ALL pre-open: 1040 adv / 890 dec / 95 unch (ratio 1.17) -> flat",
        "Bias: positive · completeness 6/6 · uncalibrated",
        "Not investment advice.",
    ]


def test_failed_sources_show_na_with_reason_never_omitted():
    signals = good_signals()
    signals["us_cash"] = failed("us_cash", "yahoo: yfinance: YFRateLimitError")
    signals["gift_gap"] = failed("gift_gap", "stale GIFT quote, 08:40 IST")
    signals["vix"] = failed("vix", "NSE allIndices: HTTP 403 after cookie refresh")
    del signals["breadth_all"]  # even a signal that was never computed keeps its line
    signals["us_futures"].detail["components"]["YM"] = None
    lines = brief_lines(MORNING_NOW, signals, composite(sources_ok=2, bias="mixed"))
    assert len(lines) == 9
    assert lines[1] == "US (prev close): n/a (yahoo: yfinance: YFRateLimitError)"
    assert lines[2] == "US futures: ES +0.12% | NQ +0.20% | YM n/a -> flat"
    assert lines[3] == "GIFT Nifty: n/a (stale GIFT quote, 08:40 IST)"
    assert lines[4] == "India VIX: n/a (NSE allIndices: HTTP 403 after cookie refresh)"
    assert lines[6] == "Breadth ALL pre-open: n/a (not computed)"
    assert lines[7] == "Bias: mixed · completeness 2/6 · uncalibrated"
    assert lines[-1] == "Not investment advice."


def test_optional_lines_and_calibrated_bias():
    lines = brief_lines(MORNING_NOW, good_signals(),
                        composite(calibrated=True, confidence_note="VIX high: lower confidence"),
                        context={"Nikkei": 0.5, "Brent": None}, summary="Cues lean positive.")
    assert "Context: Nikkei +0.50% | Brent n/a" in lines
    assert "Bias: positive · completeness 6/6 · VIX high: lower confidence" in lines
    assert lines[-2:] == ["Summary: Cues lean positive.", "Not investment advice."]


def test_html_is_escaped():
    text = to_html(brief_lines(MORNING_NOW, good_signals(), composite(), summary="<b>x</b> & y"))
    assert text.startswith("<b>Pre-open brief · 2026-11-13 09:09 IST</b>\n")
    assert "S&amp;P +0.42%" in text and "-&gt; positive" in text
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text
    assert text.count("<") == 2  # only our own <b> ... </b>
    assert text.endswith("Not investment advice.")


def test_preformatted_report_fits_telegram_limit():
    text = preformatted_html("Weekly evaluation · 2026-11-14", "row < 1 & 2\n" * 800)
    assert len(text) <= 4096 and text.endswith("</pre>\nNot investment advice.")
    assert "row &lt; 1 &amp; 2" in text


@pytest.mark.parametrize("n,text", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"),
                                    (13, "13th"), (21, "21st"), (35, "35th"), (100, "100th"), (0, "0th")])
def test_ordinal(n, text):
    assert ordinal(n) == text


def test_alert_contains_run_id_and_is_short():
    text = alert_html("morning", "abc123def456", "KeyError: '<boom>' " + "x" * 1000)
    assert "run_id abc123def456" in text and "&lt;boom&gt;" in text and len(text) < 420


# -- delivery ------------------------------------------------------------------

def test_send_telegram_posts_html(cfg):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    send_telegram("<b>hi</b>", SETTINGS, cfg, transport=httpx.MockTransport(handler))
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/bot123456:SECRET-TOKEN/sendMessage"
    assert body == {"chat_id": "42", "text": "<b>hi</b>", "parse_mode": "HTML", "disable_web_page_preview": True}


def test_send_telegram_retries_then_raises_without_leaking_token(cfg):
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        return httpx.Response(502)

    with pytest.raises(SourceError) as info:
        send_telegram("x", SETTINGS, cfg, transport=httpx.MockTransport(handler), sleep=sleeps.append, rng=lambda: 0.0)
    assert len(calls) == 1 + cfg["http"]["max_retries"]
    assert sleeps == [0.6, 1.2, 2.4]                      # exponential backoff
    assert "SECRET-TOKEN" not in str(info.value) and "HTTP 502" in str(info.value)


def test_send_telegram_does_not_retry_bad_request(cfg):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})

    with pytest.raises(SourceError, match="HTTP 400 Bad Request: can't parse entities"):
        send_telegram("x", SETTINGS, cfg, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert calls == [1]


def test_send_telegram_requires_credentials(cfg):
    with pytest.raises(SourceError, match="not set"):
        send_telegram("x", Settings(), cfg)


def test_healthcheck_ping_success_and_fail_url(cfg):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    assert ping_healthcheck("https://hc-ping.com/uuid", cfg, transport=transport)
    assert ping_healthcheck("https://hc-ping.com/uuid", cfg, ok=False, transport=transport)
    assert seen == ["https://hc-ping.com/uuid", "https://hc-ping.com/uuid/fail"]
    assert not ping_healthcheck("", cfg)

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    assert not ping_healthcheck("https://hc-ping.com/uuid", cfg, transport=httpx.MockTransport(boom))


# -- secrets -------------------------------------------------------------------

def test_secrets_never_reach_the_log_file(cfg, tmp_path):
    logger = setup_logging(cfg, SETTINGS, tmp_path)
    logger.info("POST https://api.telegram.org/bot%s/sendMessage", SETTINGS.telegram_bot_token,
                extra={"run_id": "r1", "data": {"key": SETTINGS.groq_api_key}})
    try:
        raise RuntimeError(f"failed with {SETTINGS.telegram_bot_token}")
    except RuntimeError:
        logger.exception("boom")
    for handler in logger.handlers:
        handler.flush()
    text = (tmp_path / "preopen.log").read_text()
    assert "SECRET-TOKEN" not in text and "gsk_secret" not in text and "***" in text
    records = [json.loads(line) for line in text.splitlines()]
    assert records[0]["run_id"] == "r1" and records[0]["level"] == "INFO" and "exc" in records[1]
    assert logging.getLogger("httpx").level == logging.WARNING


def test_settings_from_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=7\nLLM_PROVIDER=Gemini\nGEMINI_API_KEY=g\n"
                   "HEALTHCHECKS_URL=https://hc/x\nHEALTHCHECKS_URL_CLOSE=https://hc/close\n")
    settings = load_settings(env, environ={})
    assert (settings.telegram_bot_token, settings.telegram_chat_id, settings.llm_provider) == ("abc", "7", "gemini")
    assert settings.healthcheck_url("morning") == "https://hc/x"
    assert settings.healthcheck_url("close") == "https://hc/close"
    assert load_settings(tmp_path / "missing.env", environ={"LLM_PROVIDER": "openai"}).llm_provider == "none"


# -- LLM guard -----------------------------------------------------------------

INPUT = {"us_cash": {"label": "positive", "value": 0.4, "components": {"S&P": 0.42}},
         "gift_gap": {"label": "positive", "value": 0.32, "last": 26030.0},
         "bias": {"label": "positive", "sources_ok": 6, "sources_total": 6}}


@pytest.mark.parametrize("output,ok", [
    ("US markets closed higher with the S&P up 0.42% and GIFT Nifty at 26,030.", True),
    ("Cues lean positive; data is complete (6 of 6).", True),
    ("No numbers at all here.", True),
    ("The S&P rose 0.4%, pointing to a gap of 0.3%.", False),        # 0.3 is a rounding the input never had
    ("GIFT Nifty at 26030 suggests Nifty may open near 26100.", False),
    ("Expect a 70% chance of a positive open.", False),
    ("The Nifty 50 looks firm.", False),                              # "50" is not in the input
])
def test_number_post_check(output, ok):
    assert llm.passes_number_check(output, json.dumps(INPUT)) is ok


def groq_transport(text, calls):
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})
    return httpx.MockTransport(handler)


def test_summary_accepted_and_called_once_per_day(cfg, tmp_path):
    calls = []
    state = tmp_path / "llm_state.json"
    transport = groq_transport("Cues lean positive with the S&P up 0.42%.", calls)
    first = llm.summarize(INPUT, "2026-11-13", SETTINGS, cfg, state, transport=transport)
    assert first == "Cues lean positive with the S&P up 0.42%."
    body = json.loads(calls[0].content)
    assert json.loads(body["messages"][1]["content"]) == INPUT       # the model sees the signals JSON only
    assert "at most 3" in body["messages"][0]["content"] and calls[0].headers["authorization"] == "Bearer gsk_secret"
    assert llm.summarize(INPUT, "2026-11-13", SETTINGS, cfg, state, transport=transport) == first
    assert len(calls) == 1                                           # same day: cached, no second call
    llm.summarize(INPUT, "2026-11-16", SETTINGS, cfg, state, transport=transport)
    assert len(calls) == 2


def test_summary_with_invented_number_is_discarded(cfg, tmp_path):
    calls = []
    state = tmp_path / "llm_state.json"
    transport = groq_transport("Nifty should open near 26100.", calls)
    assert llm.summarize(INPUT, "2026-11-13", SETTINGS, cfg, state, transport=transport) is None
    assert "numbers not in the input" in json.loads(state.read_text())["status"]
    assert llm.summarize(INPUT, "2026-11-13", SETTINGS, cfg, state, transport=transport) is None
    assert len(calls) == 1                                           # a discarded answer still uses the daily call


def test_summary_too_long_or_failed_is_discarded(cfg, tmp_path):
    long = groq_transport("One. Two. Three. Four.", [])
    assert llm.summarize(INPUT, "2026-11-13", SETTINGS, cfg, tmp_path / "a.json", transport=long) is None

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert llm.summarize(INPUT, "2026-11-13", SETTINGS, cfg, tmp_path / "b.json",
                         transport=httpx.MockTransport(timeout)) is None


def test_llm_is_off_by_default(cfg, tmp_path):
    calls = []
    assert llm.summarize(INPUT, "2026-11-13", Settings(), cfg, tmp_path / "s.json",
                         transport=groq_transport("x", calls)) is None
    no_key = Settings(llm_provider="groq")
    assert llm.summarize(INPUT, "2026-11-13", no_key, cfg, tmp_path / "s.json",
                         transport=groq_transport("x", calls)) is None
    assert calls == []


def test_gemini_request_shape(cfg, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Cues lean positive."}]}}]})

    settings = Settings(llm_provider="gemini", gemini_api_key="g-key")
    out = llm.summarize(INPUT, "2026-11-13", settings, cfg, tmp_path / "s.json", transport=httpx.MockTransport(handler))
    assert out == "Cues lean positive."
    assert calls[0].headers["x-goog-api-key"] == "g-key" and "g-key" not in str(calls[0].url)
