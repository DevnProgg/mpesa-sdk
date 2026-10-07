"""Retry back-off calculation (pure, no I/O, so it is trivial to test)."""

from __future__ import annotations

import random
from collections.abc import Callable

from .settings import RetryStrategy


class RetryPolicy:
    def __init__(self, strategy: RetryStrategy, rng: Callable[[], float] = random.random) -> None:
        self._strategy = strategy
        self._rng = rng

    @property
    def max_retries(self) -> int:
        return self._strategy.retry_count

    def should_retry(self, retries_done: int) -> bool:
        return retries_done < self._strategy.retry_count

    def delay(self, retry_number: int) -> float:
        """Seconds to wait before retry ``retry_number`` (1-based)."""
        if retry_number < 1:
            raise ValueError("retry_number starts at 1")
        s = self._strategy
        base = min(s.back_off * 2 ** (retry_number - 1), s.max_back_off)
        return base + self._rng() * s.jitter
