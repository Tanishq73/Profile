"""Configuration, secrets, paths and logging.

config.yaml holds every tunable; .env holds secrets. Secrets are never logged:
a redaction filter scrubs them from every log record.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IST = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")

SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "HEALTHCHECKS_URL",
    "HEALTHCHECKS_URL_MORNING",
    "HEALTHCHECKS_URL_CLOSE",
    "HEALTHCHECKS_URL_WEEKLY",
)


@dataclass(frozen=True)
class Settings:
    """Secrets and per-machine settings from .env / the process environment."""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    llm_provider: str = "none"
    groq_api_key: str = ""
    gemini_api_key: str = ""
    healthchecks: dict[str, str] = field(default_factory=dict)

    def secrets(self) -> list[str]:
        values = [self.telegram_bot_token, self.groq_api_key, self.gemini_api_key]
        values += list(self.healthchecks.values())
        return [v for v in values if v]

    def healthcheck_url(self, job: str) -> str:
        return self.healthchecks.get(job) or self.healthchecks.get("default", "")


def load_settings(env_file: Path | None = None, environ: dict[str, str] | None = None) -> Settings:
    """Read .env (if present) overlaid by real environment variables."""
    env_file = env_file if env_file is not None else PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if env_file.exists():
        values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    values.update(os.environ if environ is None else environ)

    def get(key: str) -> str:
        return (values.get(key) or "").strip()

    provider = get("LLM_PROVIDER").lower() or "none"
    if provider not in ("groq", "gemini", "none"):
        provider = "none"
    healthchecks = {
        "default": get("HEALTHCHECKS_URL"),
        "morning": get("HEALTHCHECKS_URL_MORNING"),
        "close": get("HEALTHCHECKS_URL_CLOSE"),
        "weekly": get("HEALTHCHECKS_URL_WEEKLY"),
    }
    return Settings(
        telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=get("TELEGRAM_CHAT_ID"),
        llm_provider=provider,
        groq_api_key=get("GROQ_API_KEY"),
        gemini_api_key=get("GEMINI_API_KEY"),
        healthchecks={k: v for k, v in healthchecks.items() if v},
    )


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get("PREOPEN_CONFIG") or PROJECT_ROOT / "config.yaml")
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    return cfg


def resolve_path(cfg: dict[str, Any], key: str, override: str | Path | None = None) -> Path:
    """Resolve a configured directory relative to the project root."""
    raw = Path(override) if override else Path(cfg["paths"][key])
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


def parse_hhmm(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


def now_ist() -> datetime:
    return datetime.now(IST)


# --------------------------------------------------------------------------- logging


class RedactFilter(logging.Filter):
    """Replace any secret value that reaches a log record with ***."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.scrub(record.getMessage())
        record.args = None
        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        extra = getattr(record, "data", None)
        if extra is not None:
            record.data = json.loads(self.scrub(json.dumps(extra, default=str)))
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, scrub=lambda s: s):
        super().__init__()
        self._scrub = scrub

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("run_id", "job", "data"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self._scrub(self.formatException(record.exc_info))
        return json.dumps(entry, default=str, ensure_ascii=False)


class RunLogger(logging.LoggerAdapter):
    """Adds run_id / job to every record while keeping per-call ``extra`` (e.g. ``data``)."""

    def process(self, msg, kwargs):
        kwargs["extra"] = {**self.extra, **(kwargs.get("extra") or {})}
        return msg, kwargs


def setup_logging(
    cfg: dict[str, Any],
    settings: Settings,
    logs_dir: Path | None,
    *,
    console: bool = False,
) -> logging.Logger:
    """Structured JSON logs with rotation. ``logs_dir=None`` disables the file handler."""
    logger = logging.getLogger("preopen_brief")
    logger.setLevel(cfg["logging"]["level"])
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redact = RedactFilter(settings.secrets())
    formatter = JsonFormatter(redact.scrub)
    handlers: list[logging.Handler] = []
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                logs_dir / "preopen.log",
                maxBytes=int(cfg["logging"]["max_bytes"]),
                backupCount=int(cfg["logging"]["backup_count"]),
                encoding="utf-8",
            )
        )
    if console:
        handlers.append(logging.StreamHandler())
    if not handlers:
        handlers.append(logging.NullHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redact)
        logger.addHandler(handler)

    # httpx logs full request URLs at INFO; the Telegram URL contains the bot token.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # yfinance reports every failed ticker on stderr; failures are captured as reasons instead.
    yf_logger = logging.getLogger("yfinance")
    yf_logger.setLevel(logging.CRITICAL)
    yf_logger.propagate = False
    return logger
