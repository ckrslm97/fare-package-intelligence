"""Exponential-backoff retry with error classification.

Retryable per the spec: network errors, timeouts, HTTP 429/403, captcha, and
generic temporary errors. ``NoAvailabilityError`` is *not* an error — it is a
control signal telling the date-window loop to try the next date.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class ScraperError(Exception):
    """Base for scraper errors."""


class RetryableError(ScraperError):
    """An error that should be retried with backoff."""


class NetworkError(RetryableError):
    pass


class TimeoutErr(RetryableError):
    pass


class RateLimited(RetryableError):          # HTTP 429
    pass


class Forbidden(RetryableError):            # HTTP 403 (often bot-wall / soft block)
    pass


class CaptchaDetected(RetryableError):
    pass


class TemporaryError(RetryableError):
    pass


class NoAvailabilityError(ScraperError):
    """No flights for this (date, cabin) — try the next date. NOT retryable."""


class CarrierAbsent(NoAvailabilityError):
    """Flights exist for the OND but not for this carrier — it doesn't fly the
    route, so trying other dates in the window is pointless. Stop immediately."""


class PermanentError(ScraperError):
    """Won't be helped by retrying (bad route, unsupported carrier, parse bug)."""


_RETRYABLE_PATTERNS = re.compile(
    r"(timeout|timed out|net::|econnreset|econnrefused|socket hang up|"
    r"429|403|too many requests|forbidden|captcha|temporarily|"
    r"navigation failed|target closed|connection closed)",
    re.IGNORECASE,
)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, NoAvailabilityError) or isinstance(exc, PermanentError):
        return False
    if isinstance(exc, RetryableError):
        return True
    # Playwright / network exceptions surface as generic errors -> match by text.
    return bool(_RETRYABLE_PATTERNS.search(str(exc)))


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Full-jitter exponential backoff for ``attempt`` (0-based)."""
    raw = min(cap, base * (2 ** attempt))
    return random.uniform(0, raw)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Run ``fn`` with up to ``max_retries`` retries on retryable errors."""
    attempt = 0
    while True:
        try:
            return await fn()
        except asyncio.CancelledError:
            raise                     # never swallow cancellation
        except Exception as exc:      # noqa: BLE001 - we re-raise below
            if not is_retryable(exc) or attempt >= max_retries:
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(attempt + 1, exc, delay)
            await asyncio.sleep(delay)
            attempt += 1
