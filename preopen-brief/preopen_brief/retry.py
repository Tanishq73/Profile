"""Shared primitives: source errors, per-source results, backoff."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


class SourceError(Exception):
    """A data source failed. ``str(exc)`` is a short reason fit for the brief."""


@dataclass
class SourceResult:
    """Outcome of fetching one source. Failures are data, never exceptions."""

    name: str
    ok: bool
    data: Any = None
    error: str | None = None
    fetched_at: datetime | None = None

    @classmethod
    def failed(cls, name: str, error: str, fetched_at: datetime | None = None) -> "SourceResult":
        return cls(name=name, ok=False, error=error, fetched_at=fetched_at)


def backoff_delay(attempt: int, base: float, cap: float, rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff with full jitter on top: base * 2^(attempt-1) * (1 + U[0,1))."""
    return min(cap, base * (2 ** (attempt - 1))) * (1.0 + rng())


class Deadline:
    """Wall-clock budget for a whole run."""

    def __init__(self, seconds: float, monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self._end = monotonic() + seconds

    def remaining(self) -> float:
        return self._end - self._monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0
