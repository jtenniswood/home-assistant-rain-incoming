"""Tests for the shared HTTP retry helper."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.rain_incoming.http_retry import RateLimitBudget, fetch_with_retry


def _make_response(status: int, headers: dict | None = None) -> MagicMock:
    """Create a mock aiohttp response with the given status."""
    resp = MagicMock(spec=aiohttp.ClientResponse)
    resp.status = status
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        request_info = MagicMock()
        request_info.url = "http://example.com/test"
        resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
            request_info=request_info,
            history=(),
            status=status,
            message=f"HTTP {status}",
        )
    return resp


class TestFetchWithRetry:
    """Tests for fetch_with_retry."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        ok_resp = _make_response(200)
        session.get = AsyncMock(return_value=ok_resp)

        result = await fetch_with_retry(session, "http://example.com/test")
        assert result is ok_resp
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        rate_limited = _make_response(429)
        ok_resp = _make_response(200)

        session.get = AsyncMock(side_effect=[rate_limited, rate_limited, ok_resp])

        with patch("custom_components.rain_incoming.http_retry.asyncio.sleep", new_callable=AsyncMock):
            result = await fetch_with_retry(
                session, "http://example.com/test", max_retries=3, base_delay=0.01,
            )
        assert result is ok_resp
        assert session.get.call_count == 3

    @pytest.mark.asyncio
    async def test_respects_retry_after_header(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        rate_limited = _make_response(429, headers={"Retry-After": "2.5"})
        ok_resp = _make_response(200)

        session.get = AsyncMock(side_effect=[rate_limited, ok_resp])

        sleep_delays = []

        async def mock_sleep(delay):
            sleep_delays.append(delay)

        with patch("custom_components.rain_incoming.http_retry.asyncio.sleep", side_effect=mock_sleep):
            result = await fetch_with_retry(
                session, "http://example.com/test", max_retries=3, base_delay=1.0,
            )
        assert result is ok_resp
        assert len(sleep_delays) == 1
        assert sleep_delays[0] == 2.5

    @pytest.mark.asyncio
    async def test_retries_on_5xx_then_succeeds(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        server_error = _make_response(503)
        ok_resp = _make_response(200)

        session.get = AsyncMock(side_effect=[server_error, ok_resp])

        with patch("custom_components.rain_incoming.http_retry.asyncio.sleep", new_callable=AsyncMock):
            result = await fetch_with_retry(
                session, "http://example.com/test", max_retries=3, base_delay=0.01,
            )
        assert result is ok_resp
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        server_error = _make_response(500)

        # max_retries=2 means 3 attempts total (initial + 2 retries)
        session.get = AsyncMock(return_value=server_error)

        with patch("custom_components.rain_incoming.http_retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(aiohttp.ClientResponseError) as exc_info:
                await fetch_with_retry(
                    session, "http://example.com/test", max_retries=2, base_delay=0.01,
                )
            assert exc_info.value.status == 500

    @pytest.mark.asyncio
    async def test_does_not_retry_on_4xx_other_than_429(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        not_found = _make_response(404)

        session.get = AsyncMock(return_value=not_found)

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            await fetch_with_retry(
                session, "http://example.com/test", max_retries=3, base_delay=0.01,
            )
        assert exc_info.value.status == 404
        # Should only have called get once - no retries
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        server_error = _make_response(500)

        session.get = AsyncMock(return_value=server_error)

        sleep_delays = []

        async def mock_sleep(delay):
            sleep_delays.append(delay)

        with patch("custom_components.rain_incoming.http_retry.asyncio.sleep", side_effect=mock_sleep):
            with pytest.raises(aiohttp.ClientResponseError):
                await fetch_with_retry(
                    session, "http://example.com/test", max_retries=3, base_delay=1.0,
                )

        # 3 retries = 3 sleep calls with exponential backoff: 1, 2, 4
        assert len(sleep_delays) == 3
        assert sleep_delays[0] == 1.0
        assert sleep_delays[1] == 2.0
        assert sleep_delays[2] == 4.0

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self) -> None:
        """Verify the rate-limited fetch respects the semaphore."""
        from custom_components.rain_incoming.http_retry import rate_limited_fetch

        session = AsyncMock(spec=aiohttp.ClientSession)
        ok_resp = _make_response(200)
        session.get = AsyncMock(return_value=ok_resp)

        semaphore = asyncio.Semaphore(2)
        max_concurrent = 0
        current_concurrent = 0

        original_acquire = semaphore.acquire
        original_release = semaphore.release

        async def tracking_acquire():
            nonlocal current_concurrent, max_concurrent
            result = await original_acquire()
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            return result

        def tracking_release():
            nonlocal current_concurrent
            current_concurrent -= 1
            return original_release()

        semaphore.acquire = tracking_acquire
        semaphore.release = tracking_release

        tasks = [
            rate_limited_fetch(session, f"http://example.com/{i}", semaphore=semaphore)
            for i in range(5)
        ]
        results = await asyncio.gather(*tasks)
        assert len(results) == 5
        assert max_concurrent <= 2


class TestRateLimitBudget:
    """Tests for RateLimitBudget."""

    def test_rate_limit_budget_no_delay_under_80_percent(self) -> None:
        budget = RateLimitBudget()
        budget.update_from_headers({
            "x-ratelimit-limit": "500",
            "x-ratelimit-used": "350",
            "x-ratelimit-window": "60",
            "x-ratelimit-burst-limit": "300",
            "x-ratelimit-burst-used": "200",
        })
        # 350/500 = 70%, under 80% threshold
        assert budget.suggested_delay() == 0.0

    def test_rate_limit_budget_delay_above_80_percent(self) -> None:
        budget = RateLimitBudget()
        budget.update_from_headers({
            "x-ratelimit-limit": "500",
            "x-ratelimit-used": "450",
            "x-ratelimit-window": "60",
            "x-ratelimit-burst-limit": "300",
            "x-ratelimit-burst-used": "250",
        })
        # 450/500 = 90%, remaining=50, delay = 60/50 = 1.2s
        delay = budget.suggested_delay()
        assert delay > 0.0
        assert delay == pytest.approx(60.0 / 50, rel=1e-6)

    def test_rate_limit_budget_full_window_delay_at_limit(self) -> None:
        budget = RateLimitBudget()
        budget.update_from_headers({
            "x-ratelimit-limit": "500",
            "x-ratelimit-used": "500",
            "x-ratelimit-window": "60",
            "x-ratelimit-burst-limit": "300",
            "x-ratelimit-burst-used": "300",
        })
        # remaining=0, delay = window_seconds
        assert budget.suggested_delay() == 60.0

    def test_rate_limit_budget_no_delay_without_data(self) -> None:
        budget = RateLimitBudget()
        # No headers read yet - limit=0, should return 0.0
        assert budget.suggested_delay() == 0.0

    def test_rate_limit_budget_update_from_headers_parses_correctly(self) -> None:
        budget = RateLimitBudget()
        budget.update_from_headers({
            "x-ratelimit-limit": "500",
            "x-ratelimit-used": "123",
            "x-ratelimit-window": "60",
            "x-ratelimit-burst-limit": "300",
            "x-ratelimit-burst-used": "45",
        })
        assert budget.utilization == pytest.approx(123 / 500, rel=1e-6)
        assert budget.remaining == 500 - 123

    def test_rate_limit_budget_handles_missing_headers_gracefully(self) -> None:
        budget = RateLimitBudget()
        # Should not raise, and should return 0.0 with no data
        budget.update_from_headers({})
        assert budget.suggested_delay() == 0.0

    def test_rate_limit_budget_handles_malformed_headers(self) -> None:
        budget = RateLimitBudget()
        # Non-numeric value should not raise, should be no-op
        budget.update_from_headers({"x-ratelimit-limit": "notanumber"})
        assert budget.suggested_delay() == 0.0

    @pytest.mark.asyncio
    async def test_fetch_with_retry_updates_budget_from_response(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        ok_resp = _make_response(200, headers={
            "x-ratelimit-limit": "500",
            "x-ratelimit-used": "100",
            "x-ratelimit-window": "60",
            "x-ratelimit-burst-limit": "300",
            "x-ratelimit-burst-used": "80",
        })
        session.get = AsyncMock(return_value=ok_resp)

        budget = RateLimitBudget()
        assert budget.utilization == 0.0

        await fetch_with_retry(session, "http://example.com/test", budget=budget)

        assert budget.utilization > 0.0

    @pytest.mark.asyncio
    async def test_fetch_with_retry_adds_delay_when_suggested(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        ok_resp = _make_response(200)
        session.get = AsyncMock(return_value=ok_resp)

        budget = RateLimitBudget()
        # Pre-load budget to >80% utilization so it suggests a delay
        budget.update_from_headers({
            "x-ratelimit-limit": "500",
            "x-ratelimit-used": "460",
            "x-ratelimit-window": "60",
            "x-ratelimit-burst-limit": "300",
            "x-ratelimit-burst-used": "280",
        })
        assert budget.suggested_delay() > 0.0

        sleep_delays = []

        async def mock_sleep(delay):
            sleep_delays.append(delay)

        with patch("custom_components.rain_incoming.http_retry.asyncio.sleep", side_effect=mock_sleep):
            await fetch_with_retry(session, "http://example.com/test", budget=budget)

        # Should have slept at least once before the request due to budget
        assert len(sleep_delays) >= 1
        assert sleep_delays[0] > 0.0

    @pytest.mark.asyncio
    async def test_fetch_with_retry_without_budget_unchanged(self) -> None:
        session = AsyncMock(spec=aiohttp.ClientSession)
        ok_resp = _make_response(200)
        session.get = AsyncMock(return_value=ok_resp)

        # Without budget, should work exactly as before - no extra behavior
        result = await fetch_with_retry(session, "http://example.com/test")
        assert result is ok_resp
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_timeout_total_parameter_applied_to_request(self) -> None:
        """fetch_with_retry must accept a timeout_total parameter and apply it
        to the aiohttp request timeout instead of the hardcoded 30s default.

        Tile fetches need a shorter timeout (15s) to fail fast when RainViewer
        is slow, rather than holding connections for the full 30s.
        """
        session = AsyncMock(spec=aiohttp.ClientSession)
        ok_resp = _make_response(200)

        applied_timeout = None

        async def capturing_get(url, timeout=None, **kwargs):
            nonlocal applied_timeout
            applied_timeout = timeout
            return ok_resp

        session.get = capturing_get

        await fetch_with_retry(session, "http://example.com/tile", timeout_total=15.0)

        assert applied_timeout is not None, "timeout kwarg was not passed to session.get"
        assert applied_timeout.total == 15.0, (
            f"Expected timeout.total=15.0, got {applied_timeout.total}"
        )


async def test_query_params_survive_retries_without_logging_secrets(caplog):
    session = AsyncMock(spec=aiohttp.ClientSession)
    session.get = AsyncMock(side_effect=[_make_response(429), _make_response(200)])
    with patch("custom_components.rain_incoming.http_retry.asyncio.sleep", new_callable=AsyncMock):
        await fetch_with_retry(
            session, "https://basemaps.cartocdn.com/tile.png",
            params={"key": "secret & key"},
        )
    assert session.get.await_count == 2
    for call in session.get.call_args_list:
        assert call.kwargs["params"] == {"key": "secret & key"}
        assert "secret" not in call.args[0]
    assert "secret" not in caplog.text
    assert "Rate limited" in caplog.text
