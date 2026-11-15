"""NseClient behaviour with a mocked transport: cookies, 403 refresh, spacing, cap, retries."""

import httpx
import pytest

from preopen_brief.retry import SourceError
from preopen_brief.sources.nse import NseClient


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def make_client(cfg, handler):
    clock = FakeClock()
    times = []

    def wrapped(request):
        times.append(clock.t)
        return handler(request)

    client = NseClient(cfg, transport=httpx.MockTransport(wrapped), sleep=clock.sleep,
                       monotonic=clock.monotonic, rng=lambda: 0.0)
    return client, clock, times


def test_bootstraps_cookies_then_sends_referer(cfg):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/":
            return httpx.Response(200, headers={"set-cookie": "nsit=abc; Path=/"}, text="<html/>")
        return httpx.Response(200, json={"ok": True})

    client, _, _ = make_client(cfg, handler)
    assert client.fetch("marketStatus") == {"ok": True}
    assert [r.url.path for r in seen] == ["/", "/api/marketStatus"]
    api = seen[1]
    assert api.headers["referer"] == "https://www.nseindia.com/"
    assert "nsit=abc" in api.headers["cookie"]
    assert "Mozilla" in api.headers["user-agent"]
    client.fetch("allIndices")
    assert client.requests_made == 3  # homepage is fetched once per run


def test_403_refreshes_cookies_once_then_succeeds(cfg):
    calls = {"home": 0, "api": 0}

    def handler(request):
        if request.url.path == "/":
            calls["home"] += 1
            return httpx.Response(200, text="<html/>")
        calls["api"] += 1
        return httpx.Response(403 if calls["api"] == 1 else 200, json={"ok": True})

    client, _, _ = make_client(cfg, handler)
    assert client.fetch("marketStatus") == {"ok": True}
    assert calls == {"home": 2, "api": 2}


def test_persistent_403_marks_nse_failed_and_stops_requesting(cfg):
    def handler(request):
        return httpx.Response(200, text="<html/>") if request.url.path == "/" else httpx.Response(403)

    client, _, _ = make_client(cfg, handler)
    with pytest.raises(SourceError, match="HTTP 403 after cookie refresh"):
        client.fetch("marketStatus")
    used = client.requests_made
    assert used == 4  # home, api, home (the one refresh), api
    with pytest.raises(SourceError, match="HTTP 403 after cookie refresh"):
        client.fetch("allIndices")
    assert client.requests_made == used  # fail fast: no further requests


def test_non_json_body_is_treated_as_a_soft_block(cfg):
    def handler(request):
        return httpx.Response(200, text="<html>Access Denied</html>")

    client, _, _ = make_client(cfg, handler)
    with pytest.raises(SourceError, match="non-JSON response after cookie refresh"):
        client.fetch("marketStatus")


def test_requests_are_spaced_at_least_400ms(cfg):
    client, _, times = make_client(cfg, lambda r: httpx.Response(200, json={}))
    for name in ("marketStatus", "preopen_NIFTY", "preopen_ALL", "allIndices"):
        client.fetch(name)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) == 5 and min(gaps) >= 0.4


def test_request_cap_is_enforced(cfg):
    cfg["nse"]["max_requests_per_run"] = 3
    client, _, times = make_client(cfg, lambda r: httpx.Response(200, json={}))
    client.fetch("marketStatus")
    client.fetch("allIndices")
    with pytest.raises(SourceError, match="request cap"):
        client.fetch("preopen_ALL")
    assert len(times) == 3


def test_default_morning_run_stays_within_ten_requests_even_when_blocked(cfg):
    client, _, times = make_client(cfg, lambda r: httpx.Response(403))
    for name in ("holiday_master", "marketStatus", "preopen_NIFTY", "preopen_ALL", "allIndices"):
        with pytest.raises(SourceError):
            client.fetch(name)
    assert len(times) <= 10


def test_retries_5xx_with_backoff_then_gives_up(cfg):
    def handler(request):
        return httpx.Response(200, text="") if request.url.path == "/" else httpx.Response(503)

    client, clock, times = make_client(cfg, handler)
    with pytest.raises(SourceError, match="HTTP 503"):
        client.fetch("marketStatus")
    assert len(times) == 1 + 1 + cfg["nse"]["max_retries"]
    backoffs = [s for s in clock.sleeps if s >= cfg["http"]["backoff_base"]]
    assert backoffs == sorted(backoffs) and len(backoffs) == cfg["nse"]["max_retries"]


def test_timeout_is_retried_and_reported(cfg):
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, text="")
        raise httpx.ReadTimeout("slow", request=request)

    client, _, _ = make_client(cfg, handler)
    with pytest.raises(SourceError, match="network error \\(ReadTimeout\\)"):
        client.fetch("marketStatus")


def test_recovers_after_transient_error(cfg):
    state = {"n": 0}

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, text="")
        state["n"] += 1
        return httpx.Response(502) if state["n"] == 1 else httpx.Response(200, json={"v": 1})

    client, _, _ = make_client(cfg, handler)
    assert client.fetch("marketStatus") == {"v": 1}


def test_404_is_not_retried(cfg):
    client, _, times = make_client(cfg, lambda r: httpx.Response(200 if r.url.path == "/" else 404))
    with pytest.raises(SourceError, match="HTTP 404"):
        client.fetch("marketStatus")
    assert len(times) == 2
