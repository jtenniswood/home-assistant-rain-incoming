"""Integration tests for RadarImageEntity greedy rendering.

TDD: tests written FIRST. Initially red against the lazy implementation,
then green after _handle_coordinator_update / _render_to_cache are added.

Red → green cycle documented for tests 1, 4, and 5 as required.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rain_incoming.image import RadarImageEntity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_coordinator(
    latest_frame_path: str | None = "/v2/radar/latest",
    frame_paths: list[str] | None = None,
    map_style: str = "voyager",
):
    coord = MagicMock()
    coord.latest_frame_path = latest_frame_path
    coord.frame_paths = frame_paths or ["/v2/radar/f1", "/v2/radar/f2", "/v2/radar/latest"]
    coord.frame_timestamps = [
        datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 10, 10, 10, tzinfo=timezone.utc),
        datetime(2026, 4, 10, 10, 20, tzinfo=timezone.utc),
    ]
    coord.confidence_maps = None
    coord.tracked_cells = []
    coord.last_update_success = True
    coord.last_update_success_time = datetime.now(timezone.utc)
    coord.map_style = map_style
    coord.data = MagicMock()
    return coord


def _make_entry():
    entry = MagicMock()
    entry.data = {
        "latitude": -33.7,
        "longitude": 151.2,
    }
    entry.entry_id = "test_entry_greedy"
    return entry


def _make_entity(
    latest_frame_path: str | None = "/v2/radar/latest",
    frame_paths: list[str] | None = None,
) -> RadarImageEntity:
    coord = _make_coordinator(latest_frame_path=latest_frame_path, frame_paths=frame_paths)
    entry = _make_entry()
    entity = RadarImageEntity(coord, entry, 128)

    # Attach a minimal mock hass
    hass = MagicMock()
    hass.config.latitude = -33.7
    hass.config.longitude = 151.2
    hass.config.time_zone = "Australia/Sydney"
    entity.hass = hass
    return entity


# ---------------------------------------------------------------------------
# Test 1 (RED first): coordinator update schedules a render
# ---------------------------------------------------------------------------

class TestHandleCoordinatorUpdateSchedulesRender:
    """_handle_coordinator_update must schedule a background render task.

    RED: passes before implementation because _handle_coordinator_update
    does NOT call _schedule_render in the original lazy code.
    """

    @pytest.mark.asyncio
    async def test_handle_coordinator_update_schedules_render(self):
        """Triggering a coordinator update must cause render_animated_composite
        to be called (via the scheduled background task)."""
        entity = _make_entity()

        # Track render calls
        render_called = asyncio.Event()

        async def fake_render(*args, **kwargs):
            render_called.set()
            return b"GIF89a_fake"

        with patch(
            "custom_components.rain_incoming.image.render_animated_composite",
            new=fake_render,
        ):
            with patch("custom_components.rain_incoming.image.async_get_clientsession", return_value=MagicMock()):
                # Capture tasks created via hass.async_create_task.
                # async_create_task is synchronous in HA - it wraps the coroutine
                # into a Task and returns it.
                created_tasks: list[asyncio.Task] = []

                def capture_task(coro, *, name=None):
                    task = asyncio.ensure_future(coro)
                    created_tasks.append(task)
                    return task

                entity.hass.async_create_task = capture_task

                # async_write_ha_state requires a real HA entity registration; patch it
                # out since we're only testing that a render task gets scheduled.
                with patch.object(entity, "async_write_ha_state"):
                    entity._handle_coordinator_update()

                # Wait for all created tasks to complete
                if created_tasks:
                    await asyncio.gather(*created_tasks, return_exceptions=True)

                assert render_called.is_set(), (
                    "_handle_coordinator_update must schedule a background render; "
                    "render_animated_composite was never called"
                )


# ---------------------------------------------------------------------------
# Test 2: async_image returns cached bytes immediately (no rendering)
# ---------------------------------------------------------------------------

class TestAsyncImageReturnsCachedBytes:
    """When cache is warm, async_image must return bytes without rendering."""

    @pytest.mark.asyncio
    async def test_async_image_returns_cached_bytes_when_warm(self):
        entity = _make_entity()
        entity._cached_image = b"GIF89a_from_cache"

        with patch(
            "custom_components.rain_incoming.image.render_animated_composite",
        ) as mock_render:
            result = await entity.async_image()

        assert result == b"GIF89a_from_cache"
        mock_render.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: placeholder on cold start
# ---------------------------------------------------------------------------

class TestAsyncImageReturnsPlaceholderWhenCold:
    """Fresh entity with no cache or data returns a valid GIF placeholder."""

    @pytest.mark.asyncio
    async def test_async_image_returns_placeholder_when_cold(self):
        entity = _make_entity(latest_frame_path=None)

        result = await entity.async_image()

        assert result is not None, "Must return placeholder bytes, not None"
        assert result[:3] == b"GIF", (
            "Placeholder must be a valid GIF (starts with GIF signature)"
        )
        assert len(result) > 100, "Placeholder GIF must have non-trivial content"


# ---------------------------------------------------------------------------
# Test 4 (RED first): render timeout falls back to placeholder
# ---------------------------------------------------------------------------

class TestRenderTimeoutFallback:
    """When render_animated_composite takes longer than the timeout,
    the cache must NOT be populated and async_image must return placeholder.

    RED: old code had no asyncio.wait_for around the render, so a slow
    render would just block forever rather than timing out gracefully.
    """

    @pytest.mark.asyncio
    async def test_render_timeout_returns_placeholder(self):
        entity = _make_entity()
        # No previous cache
        entity._cached_image = None

        async def slow_render(*args, **kwargs):
            await asyncio.sleep(60)
            return b"GIF89a_too_late"

        with patch(
            "custom_components.rain_incoming.image.render_animated_composite",
            new=slow_render,
        ):
            with patch(
                "custom_components.rain_incoming.image._RENDER_TIMEOUT_SECONDS",
                1.0,  # speed up the test
            ):
                with patch("custom_components.rain_incoming.image.async_get_clientsession", return_value=MagicMock()):
                    await entity._render_to_cache()

        # Cache must NOT have been populated
        assert entity._cached_image is None, (
            "Cache must not be populated when render timed out"
        )
        assert entity._cached_frame_path is None, (
            "On timeout, _cached_frame_path must NOT be updated, otherwise the "
            "next coordinator update would skip rendering this frame"
        )

        # async_image must fall back to placeholder
        result = await entity.async_image()
        assert result is not None
        assert result[:3] == b"GIF", "Timeout fallback must return a valid GIF"


# ---------------------------------------------------------------------------
# Test 5 (RED first): double-render guard
# ---------------------------------------------------------------------------

class TestDoubleRenderGuard:
    """Calling _schedule_render twice before the first task finishes must
    NOT create a second task.

    RED: old code had no _render_task tracking at all.
    """

    @pytest.mark.asyncio
    async def test_double_render_guard(self):
        entity = _make_entity()

        render_start = asyncio.Event()
        render_hold = asyncio.Event()

        async def slow_render(*args, **kwargs):
            render_start.set()
            await render_hold.wait()
            return b"GIF89a_held"

        with patch(
            "custom_components.rain_incoming.image.render_animated_composite",
            new=slow_render,
        ):
            with patch("custom_components.rain_incoming.image.async_get_clientsession", return_value=MagicMock()):
                tasks_created: list[asyncio.Task] = []

                def capture_task(coro, *, name=None):
                    task = asyncio.ensure_future(coro)
                    tasks_created.append(task)
                    return task

                entity.hass.async_create_task = capture_task

                # First schedule
                entity._schedule_render()
                # Wait for the render to start so the task is definitely running
                await render_start.wait()

                # Second schedule - should be a no-op
                entity._schedule_render()

                assert len(tasks_created) == 1, (
                    f"Expected exactly 1 render task, got {len(tasks_created)}. "
                    "Double-render guard must prevent duplicate tasks."
                )

                # Release the held render task so it completes cleanly
                render_hold.set()
                if tasks_created:
                    await asyncio.gather(*tasks_created, return_exceptions=True)


# ---------------------------------------------------------------------------
# Test 6: render failure logs error and does not update cache
# ---------------------------------------------------------------------------

class TestRenderFailureLogsError:
    """render_animated_composite raising RuntimeError must log at ERROR
    and leave the cache unchanged."""

    @pytest.mark.asyncio
    async def test_render_failure_logs_error(self, caplog):
        entity = _make_entity()
        entity._cached_image = None

        with patch(
            "custom_components.rain_incoming.image.render_animated_composite",
            side_effect=RuntimeError("tile fetch exploded"),
        ):
            with patch("custom_components.rain_incoming.image.async_get_clientsession", return_value=MagicMock()):
                with caplog.at_level(logging.ERROR, logger="custom_components.rain_incoming.image"):
                    await entity._render_to_cache()

        assert entity._cached_image is None, "Cache must not be populated after render failure"
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "render failed" in r.getMessage().lower() for r in error_records
        ), f"Expected an ERROR log mentioning 'render failed', got: {[r.getMessage() for r in error_records]}"


# ---------------------------------------------------------------------------
# Test 7: async_added_to_hass triggers render when data already present
# ---------------------------------------------------------------------------

class TestAsyncAddedToHassTriggersRender:
    """If the coordinator already has frame data when the entity is added to
    hass, async_added_to_hass must schedule an immediate render."""

    @pytest.mark.asyncio
    async def test_async_added_to_hass_triggers_render_when_data_present(self):
        entity = _make_entity(latest_frame_path="/v2/radar/existing")

        tasks_created: list[asyncio.Task] = []

        def capture_task(coro, *, name=None):
            task = asyncio.ensure_future(coro)
            tasks_created.append(task)
            return task

        entity.hass.async_create_task = capture_task

        # async_added_to_hass calls super().async_added_to_hass() which calls
        # coordinator.async_add_listener - mock that out
        entity.coordinator.async_add_listener = MagicMock(return_value=lambda: None)
        entity.coordinator.last_update_success = True
        entity.coordinator.last_update_success_time = datetime.now(timezone.utc)

        with patch(
            "custom_components.rain_incoming.image.render_animated_composite",
            new=AsyncMock(return_value=b"GIF89a_preload"),
        ):
            with patch("custom_components.rain_incoming.image.async_get_clientsession", return_value=MagicMock()):
                await entity.async_added_to_hass()
                # Let scheduled tasks run
                if tasks_created:
                    await asyncio.gather(*tasks_created, return_exceptions=True)

        assert len(tasks_created) == 1, (
            f"Expected exactly 1 render task, got {len(tasks_created)}"
        )


# ---------------------------------------------------------------------------
# Test 8: async_added_to_hass does NOT render when no data present
# ---------------------------------------------------------------------------

class TestAsyncAddedToHassNoRenderWhenNoData:
    """If the coordinator has no frame data yet, async_added_to_hass must
    NOT schedule a render (nothing to render)."""

    @pytest.mark.asyncio
    async def test_async_added_to_hass_does_not_render_when_data_absent(self):
        entity = _make_entity(latest_frame_path=None)

        tasks_created: list[asyncio.Task] = []

        def capture_task(coro, *, name=None):
            task = asyncio.ensure_future(coro)
            tasks_created.append(task)
            return task

        entity.hass.async_create_task = capture_task
        entity.coordinator.async_add_listener = MagicMock(return_value=lambda: None)

        await entity.async_added_to_hass()
        # Drain pending event loop callbacks - this is NOT a timing wait;
        # it's the standard idiom for letting scheduled coroutines start.
        await asyncio.sleep(0)

        assert len(tasks_created) == 0, (
            "async_added_to_hass must not schedule a render when coordinator has no data"
        )


# ---------------------------------------------------------------------------
# Test 9: render skipped when frame path unchanged
# ---------------------------------------------------------------------------

class TestRenderSkippedWhenFramePathUnchanged:
    """_render_to_cache must be a no-op when the frame hasn't changed."""

    @pytest.mark.asyncio
    async def test_render_skipped_when_frame_path_unchanged(self):
        entity = _make_entity(latest_frame_path="/v2/radar/latest")
        entity._cached_image = b"GIF89a_already_rendered"
        entity._cached_frame_path = "/v2/radar/latest"  # same as coordinator

        # _render_to_cache should short-circuit before calling async_get_clientsession
        # or render_animated_composite when the frame path hasn't changed.
        with patch(
            "custom_components.rain_incoming.image.render_animated_composite",
        ) as mock_render:
            with patch("custom_components.rain_incoming.image.async_get_clientsession"):
                await entity._render_to_cache()

        mock_render.assert_not_called()
        assert entity._cached_image == b"GIF89a_already_rendered"


# ---------------------------------------------------------------------------
# Test 10: render task cancelled on entity removal
# ---------------------------------------------------------------------------

class TestRenderTaskCancelledOnEntityRemoval:
    """async_will_remove_from_hass must cancel any in-flight render task."""

    @pytest.mark.asyncio
    async def test_render_task_cancelled_on_entity_removal(self):
        entity = _make_entity()

        render_hold = asyncio.Event()

        async def slow_render(*args, **kwargs):
            await render_hold.wait()
            return b"GIF89a_held"

        with patch(
            "custom_components.rain_incoming.image.render_animated_composite",
            new=slow_render,
        ):
            with patch("custom_components.rain_incoming.image.async_get_clientsession", return_value=MagicMock()):
                def capture_task(coro, *, name=None):
                    task = asyncio.ensure_future(coro)
                    entity._render_task = task
                    return task

                entity.hass.async_create_task = capture_task
                entity._schedule_render()

                # Give the event loop a tick so the task actually starts
                # Drain pending event loop callbacks - this is NOT a timing wait;
                # it's the standard idiom for letting scheduled coroutines start.
                await asyncio.sleep(0)

                task = entity._render_task
                assert task is not None, "A render task must have been created"
                assert not task.done(), "Task should still be running"

                await entity.async_will_remove_from_hass()

                # Give the event loop ticks to propagate the cancellation
                # through the task's coroutine stack.
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

        assert task.cancelled() or task.done(), (
            "async_will_remove_from_hass must cancel the in-flight render task"
        )


# ---------------------------------------------------------------------------
# Fix 2: Concurrent renders across radii must be serialized PER LOCATION
# ---------------------------------------------------------------------------

class TestRenderSerialization:
    """Render tasks for the SAME location must not run concurrently.

    With 3 radii (64, 128, 256km), all render tasks fire when the coordinator
    updates. Without serialization they run simultaneously, multiplying
    connection load by 3x and causing HA-wide lockups when tiles are slow.

    Fix: a per-coordinator asyncio.Lock serializes renders within one
    location. Different locations can render in parallel (#144).
    """

    @pytest.mark.asyncio
    async def test_same_location_renders_are_serialized(self):
        """Two _render_to_cache calls for the same coordinator must not
        execute render_animated_composite simultaneously."""
        coord = _make_coordinator()
        entry = _make_entry()
        entity_128 = RadarImageEntity(coord, entry, 128)
        entity_256 = RadarImageEntity(coord, entry, 256)

        hass = MagicMock()
        hass.config.latitude = -33.7
        hass.config.longitude = 151.2
        hass.config.time_zone = "Australia/Sydney"
        entity_128.hass = hass
        entity_256.hass = hass

        concurrent_count = 0
        max_concurrent = 0

        async def tracked_render(*args, **kwargs):
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            concurrent_count -= 1
            return b"GIF89a_test"

        with (
            patch(
                "custom_components.rain_incoming.image.render_animated_composite",
                new=tracked_render,
            ),
            patch(
                "custom_components.rain_incoming.image.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            await asyncio.gather(
                entity_128._render_to_cache(),
                entity_256._render_to_cache(),
            )

        assert max_concurrent == 1, (
            f"Same-location renders ran concurrently (max={max_concurrent}). "
            "Per-coordinator render lock must serialize renders within one location."
        )

    @pytest.mark.asyncio
    async def test_different_locations_render_in_parallel(self):
        """Two _render_to_cache calls for DIFFERENT coordinators must run
        concurrently. The global lock previously starved 256km entities
        when multiple locations were configured (#144)."""
        coord_a = _make_coordinator()
        coord_b = _make_coordinator()
        entry_a = _make_entry()
        entry_b = MagicMock()
        entry_b.data = {"latitude": -37.8, "longitude": 144.9}
        entry_b.entry_id = "test_entry_b"

        entity_a = RadarImageEntity(coord_a, entry_a, 128)
        entity_b = RadarImageEntity(coord_b, entry_b, 128)

        hass = MagicMock()
        hass.config.latitude = -33.7
        hass.config.longitude = 151.2
        hass.config.time_zone = "Australia/Sydney"
        entity_a.hass = hass
        entity_b.hass = hass

        concurrent_count = 0
        max_concurrent = 0

        async def tracked_render(*args, **kwargs):
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            concurrent_count -= 1
            return b"GIF89a_test"

        with (
            patch(
                "custom_components.rain_incoming.image.render_animated_composite",
                new=tracked_render,
            ),
            patch(
                "custom_components.rain_incoming.image.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            await asyncio.gather(
                entity_a._render_to_cache(),
                entity_b._render_to_cache(),
            )

        assert max_concurrent == 2, (
            f"Different-location renders did not run in parallel (max={max_concurrent}). "
            "Per-coordinator locks must allow different locations to render concurrently."
        )


# ---------------------------------------------------------------------------
# Fix 4: async_image must never await the render task (always non-blocking)
# ---------------------------------------------------------------------------

class TestAsyncImageNeverBlocks:
    """async_image() must return immediately in ALL cases — warm cache, cold
    cache, or render in flight.

    HA's image_proxy handler has a ~60s timeout. Awaiting the render task
    (which can take up to 55s and shares a global lock with two other radius
    entities) easily exceeds that and causes HA to return HTTP 500. The fix:
    always return from cache if warm, otherwise return the placeholder and let
    the background render task populate the cache.

    The test detects blocking by attaching a never-resolving task and wrapping
    async_image() in asyncio.wait_for(timeout=0.1). If the task is awaited,
    wait_for raises TimeoutError (RED). If the function returns immediately,
    the test passes (GREEN).
    """

    @pytest.mark.asyncio
    async def test_async_image_does_not_await_render_task_when_cache_warm(self):
        entity = _make_entity()
        entity._cached_image = b"GIF89a_cached"

        never_done: asyncio.Future = asyncio.get_event_loop().create_future()

        async def blocking_coro():
            await never_done

        task = asyncio.create_task(blocking_coro())
        entity._render_task = task

        try:
            result = await asyncio.wait_for(entity.async_image(), timeout=0.1)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        assert result == b"GIF89a_cached", (
            "async_image must return cached bytes immediately when cache is warm"
        )

    @pytest.mark.asyncio
    async def test_async_image_returns_placeholder_on_cold_start_without_blocking(self):
        """Cold start with a running render task: must return placeholder immediately.

        OLD behavior: await self._render_task (blocks up to 55s → HA returns 500).
        NEW behavior: return placeholder immediately; real image available after
        the next request once the background render completes.
        """
        entity = _make_entity()
        entity._cached_image = None  # cold start

        never_done: asyncio.Future = asyncio.get_event_loop().create_future()

        async def blocking_coro():
            await never_done

        task = asyncio.create_task(blocking_coro())
        entity._render_task = task

        try:
            result = await asyncio.wait_for(entity.async_image(), timeout=0.1)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        assert result is not None, "Must return placeholder bytes, not None"
        assert result[:3] == b"GIF", (
            "Cold start must return placeholder GIF immediately, not block on render"
        )

    @pytest.mark.asyncio
    async def test_real_image_served_after_render_completes(self):
        """Inverse: after the render task populates the cache, the next call
        to async_image() returns the real image.

        Proves that returning a placeholder on cold start is temporary — the
        real radar image IS served once the background render finishes.
        """
        entity = _make_entity()
        entity._cached_image = None

        real_image = b"GIF89a_real_radar_data"
        render_done = asyncio.Event()

        async def render_and_populate():
            await asyncio.sleep(0.02)
            entity._cached_image = real_image
            render_done.set()

        task = asyncio.create_task(render_and_populate())
        entity._render_task = task

        # First call (cold start): returns placeholder
        first = await entity.async_image()
        assert first[:3] == b"GIF"
        assert first != real_image, "Cold start must return placeholder, not real image"

        # Wait for render to complete
        await render_done.wait()
        await task

        # Second call (cache warm): returns real image
        second = await entity.async_image()
        assert second == real_image, (
            "After render completes, async_image must serve the real radar image"
        )


# ---------------------------------------------------------------------------
# Fix 3b: Render timeout must be 55s
# ---------------------------------------------------------------------------

class TestRenderTimeout:
    """Render timeout must be 55s — long enough for slow-but-successful
    tile fetches (up to 15s each) while still staying below HA's ~60s
    image_proxy timeout."""

    def test_render_timeout_is_55_seconds(self):
        from custom_components.rain_incoming.image import _RENDER_TIMEOUT_SECONDS

        assert _RENDER_TIMEOUT_SECONDS == 55.0, (
            f"Render timeout must be 55s to give more headroom for slow tiles "
            f"while staying under HA's ~60s proxy timeout. Got {_RENDER_TIMEOUT_SECONDS}s."
        )


async def test_render_uses_saved_carto_key():
    entity = _make_entity()
    entity._entry.data["carto_api_key"] = "saved-key"
    with patch(
        "custom_components.rain_incoming.image.render_animated_composite",
        new_callable=AsyncMock, return_value=b"GIF89a_test",
    ) as render, patch(
        "custom_components.rain_incoming.image.async_get_clientsession",
        return_value=MagicMock(),
    ):
        await entity._render_to_cache()
    assert render.call_args.kwargs["carto_api_key"] == "saved-key"
    assert entity._cached_image == b"GIF89a_test"
