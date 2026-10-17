"""Shared HTTP retry helper with backoff and rate-limit awareness."""
from __future__ import annotations

import asyncio
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Default concurrency limit for tile fetches
DEFAULT_CONCURRENT_LIMIT = 10

_UTILIZATION_THRESHOLD = 0.80


class RateLimitBudget:
    """Tracks RainViewer rate limit state from response headers."""

    def __init__(self) -> None:
        self._limit: int = 0
        self._used: int = 0
        self._window_seconds: float = 0.0
        self._burst_limit: int = 0
        self._burst_used: int = 0

    def update_from_headers(self, headers) -> None:
        """Parse x-ratelimit-* headers. Missing/malformed headers: no-op."""
        try:
            limit = int(headers["x-ratelimit-limit"])
            used = int(headers["x-ratelimit-used"])
            window = float(headers["x-ratelimit-window"])
        except (KeyError, ValueError, TypeError):
            return

        self._limit = limit
        self._used = used
        self._window_seconds = window

        try:
            self._burst_limit = int(headers["x-ratelimit-burst-limit"])
        except (KeyError, ValueError, TypeError):
            pass

        try:
            self._burst_used = int(headers["x-ratelimit-burst-used"])
        except (KeyError, ValueError, TypeError):
            pass

    @property
    def utilization(self) -> float:
        """used / limit, 0.0 if no data yet."""
        if self._limit == 0:
            return 0.0
        return self._used / self._limit

    @property
    def remaining(self) -> int:
        """limit - used, 0 if no data yet."""
        if self._limit == 0:
            return 0
        return max(0, self._limit - self._used)

    def suggested_delay(self) -> float:
        """Inter-request delay in seconds.

        - Under 80% utilization: 0.0
        - 80-100%: window_seconds / remaining (spread remaining budget evenly)
        - At limit (remaining=0): window_seconds (wait for reset)
        - No data yet (limit=0): 0.0
        """
        if self._limit == 0:
            return 0.0
        if self.utilization < _UTILIZATION_THRESHOLD:
            return 0.0
        remaining = self.remaining
        if remaining == 0:
            return self._window_seconds
        return self._window_seconds / remaining


def _get_retry_delay(
    resp: aiohttp.ClientResponse,
    attempt: int,
    base_delay: float,
) -> float:
    """Compute the retry delay for a retryable response.

    For 429 responses, respects the Retry-After header if present and numeric.
    For all other statuses, uses exponential backoff.
    """
    delay = base_delay * (2 ** attempt)
    if resp.status == 429:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                _LOGGER.debug(
                    "Non-numeric Retry-After header '%s', using exponential backoff",
                    retry_after,
                )
    return delay


def _is_retryable_status(status: int) -> bool:
    """Return True for HTTP statuses that should trigger a retry."""
    return status == 429 or status >= 500


async def _apply_rate_limit_pacing(budget: RateLimitBudget | None) -> None:
    """Sleep if the rate limit budget says we should slow down."""
    if budget is None:
        return
    delay = budget.suggested_delay()
    if delay > 0:
        _LOGGER.info(
            "Rate limit pacing: sleeping %.2fs before request (utilization %.0f%%)",
            delay, budget.utilization * 100,
        )
        await asyncio.sleep(delay)


async def _handle_timeout(url: str, attempt: int, max_retries: int, base_delay: float) -> bool:
    """Log and sleep after a timeout.

    Returns True if the caller should retry, False if retries are exhausted
    (caller must re-raise the original exception).
    """
    if attempt < max_retries:
        delay = base_delay * (2 ** attempt)
        _LOGGER.warning(
            "Timeout on %s, retrying in %.1fs (attempt %d/%d)",
            url, delay, attempt + 1, max_retries + 1,
        )
        await asyncio.sleep(delay)
        return True
    _LOGGER.warning("All %d retries exhausted (timeout) for %s", max_retries, url)
    return False


async def _handle_retryable_status(
    resp: aiohttp.ClientResponse,
    url: str,
    attempt: int,
    max_retries: int,
    base_delay: float,
) -> bool:
    """Handle a retryable HTTP status (429/5xx).

    Returns True if the caller should continue to the next attempt,
    or raises / calls raise_for_status() if retries are exhausted.
    """
    if attempt < max_retries:
        delay = _get_retry_delay(resp, attempt, base_delay)
        _LOGGER.warning(
            "%s (%d) on %s, retrying in %.1fs (attempt %d/%d)",
            "Rate limited" if resp.status == 429 else "Server error",
            resp.status, url, delay, attempt + 1, max_retries + 1,
        )
        resp.release()
        await asyncio.sleep(delay)
        return True
    _LOGGER.warning(
        "All %d retries exhausted (%d) for %s",
        max_retries, resp.status, url,
    )
    resp.raise_for_status()
    return False  # raise_for_status always raises; satisfies type checker


async def fetch_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    budget: RateLimitBudget | None = None,
    timeout_total: float = 30.0,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> aiohttp.ClientResponse:
    """Fetch a URL with retry on 429/5xx, respecting Retry-After headers.

    Returns the successful response. Raises aiohttp.ClientResponseError if all
    retries are exhausted or a non-retryable error status is returned.

    timeout_total: per-request timeout in seconds. Default 30s. Tile fetches
        use 15s to fail fast when the tile server is slow.
    """
    request_timeout = aiohttp.ClientTimeout(total=timeout_total)
    for attempt in range(max_retries + 1):
        await _apply_rate_limit_pacing(budget)

        try:
            resp = await session.get(
                url, timeout=request_timeout, headers=headers,
                **({"params": params} if params is not None else {}),
            )
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            if await _handle_timeout(url, attempt, max_retries, base_delay):
                continue
            raise

        if budget is not None:
            budget.update_from_headers(resp.headers)

        if _is_retryable_status(resp.status):
            should_retry = await _handle_retryable_status(resp, url, attempt, max_retries, base_delay)
            if should_retry:
                continue

        # Non-retryable status (success or 4xx other than 429)
        resp.raise_for_status()
        return resp

    # Should not reach here, but satisfy type checker
    raise RuntimeError("Unreachable: retry loop exited without return or raise")


async def rate_limited_fetch(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore | None = None,
    **kwargs,
) -> aiohttp.ClientResponse:
    """Fetch with retry, gated by a semaphore for concurrency control."""
    if semaphore is None:
        return await fetch_with_retry(session, url, **kwargs)
    async with semaphore:
        return await fetch_with_retry(session, url, **kwargs)
