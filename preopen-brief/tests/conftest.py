"""Shared fixtures. The whole suite runs without network: sockets are blocked."""

from __future__ import annotations

import copy
import json
import socket
from datetime import datetime
from pathlib import Path

import pytest

from preopen_brief import notify
from preopen_brief.config import IST, Settings, load_config
from preopen_brief.jobs import common
from preopen_brief.tradingday import Calendar, load_calendar

FIXTURES = Path(__file__).parent / "fixtures"
MORNING_NOW = datetime(2026, 11, 13, 9, 9, 5, tzinfo=IST)
CLOSE_NOW = datetime(2026, 11, 13, 15, 32, 10, tzinfo=IST)


def load_fixture(directory: str, name: str):
    with open(FIXTURES / directory / f"{name}_SYNTHETIC.json", encoding="utf-8") as fh:
        return json.load(fh)


def ist(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=IST)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    """Capture Telegram sends / healthcheck pings and keep any local .env out of the tests."""
    sent: list[str] = []
    pings: list[tuple[str, bool]] = []
    settings = Settings(telegram_bot_token="123456:TEST-TOKEN", telegram_chat_id="42",
                        healthchecks={"default": "https://hc.example/ping/abc"})
    monkeypatch.setattr(common, "load_settings", lambda: settings)
    monkeypatch.setattr(notify, "send_telegram", lambda text, s, c, **kw: sent.append(text))
    monkeypatch.setattr(notify, "ping_healthcheck", lambda url, c, ok=True, **kw: pings.append((url, ok)) or True)
    return {"sent": sent, "pings": pings, "settings": settings}


@pytest.fixture(scope="session")
def base_cfg():
    return load_config()


@pytest.fixture
def cfg(base_cfg):
    return copy.deepcopy(base_cfg)


@pytest.fixture
def calendar(cfg, tmp_path) -> Calendar:
    return load_calendar(MORNING_NOW, tmp_path, cfg, lambda: load_fixture("morning", "holiday_master"))
