from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from homeassistant.components.image import ImageEntity

_LOGGER = logging.getLogger(__name__)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CARTO_API_KEY, CONF_LOCATION_NAME, DOMAIN, RADAR_GIF_FRAME_DURATION_MS, RADAR_RADII_KM
from .coordinator import RainDetectorCoordinator
from .entity_helpers import device_info_from_entry
from .radar.composite import render_animated_composite

# Per-render timeout: set below HA image_proxy's ~60s default so a slow render
# falls back gracefully to the placeholder instead of letting image_proxy time out
# and return a broken-image icon.
_RENDER_TIMEOUT_SECONDS = 55.0

# Render locks: one per coordinator (config entry), keyed by entry_id.
# Multiple radii (64/128/256km) for the SAME location serialize behind one
# lock to prevent tile fetch storms. Different locations render in parallel -
# a global lock starved 256km entities when multiple locations were configured
# (#144).
_render_locks: dict[str, asyncio.Lock] = {}


def _get_render_lock(entry_id: str) -> asyncio.Lock:
    """Get or create the render lock for a config entry."""
    if entry_id not in _render_locks:
        _render_locks[entry_id] = asyncio.Lock()
    return _render_locks[entry_id]


def reset_render_lock(entry_id: str | None = None) -> None:
    """Reset render lock(s). Called when a config entry is unloaded.

    If entry_id is provided, removes just that entry's lock.
    If None, removes all locks (backward compat for last-entry-unload).
    """
    if entry_id is not None:
        _render_locks.pop(entry_id, None)
    else:
        _render_locks.clear()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: RainDetectorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        RadarImageEntity(coordinator, entry, radius_km)
        for radius_km in RADAR_RADII_KM
    ])


class RadarImageEntity(CoordinatorEntity[RainDetectorCoordinator], ImageEntity):
    """Image entity showing a radar composite map."""

    _attr_has_entity_name = True
    _attr_content_type = "image/gif"

    def __init__(self, coordinator: RainDetectorCoordinator, entry: ConfigEntry, radius_km: int) -> None:
        super().__init__(coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._entry = entry
        self._radius_km = radius_km
        self._attr_name = f"Radar {radius_km}km"
        self._attr_unique_id = f"{entry.entry_id}_radar_{radius_km}km"
        self._cached_image: bytes | None = None
        self._cached_frame_path: str | None = None
        self._render_task: asyncio.Task | None = None
        self._created_at: datetime = datetime.now(timezone.utc)

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_from_entry(self._entry)

    @property
    def image_last_updated(self) -> datetime | None:
        # Must never return None - HA's image proxy won't generate a valid URL
        # without a timestamp, so async_image() would never be called and the
        # frontend would show a broken-image icon instead of the placeholder.
        return self.coordinator.last_update_success_time or self._created_at

    @property
    def available(self) -> bool:
        # Always available - we show cached data or a placeholder
        return True

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {}
        if (
            self._cached_image is not None
            and not self.coordinator.last_update_success
            and self.coordinator.last_update_success_time is not None
        ):
            attrs["stale_since"] = self.coordinator.last_update_success_time.isoformat()
        return attrs

    # ------------------------------------------------------------------
    # Greedy rendering - pre-render on every coordinator update
    # ------------------------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Called by CoordinatorEntity whenever coordinator data refreshes.

        Calls the parent to update internal HA state, then schedules a
        background render so the cache is warm before the next frontend request.
        """
        super()._handle_coordinator_update()
        self._schedule_render()

    def _schedule_render(self) -> None:
        """Start a background render task if one isn't already running."""
        if self._render_task is not None and not self._render_task.done():
            _LOGGER.debug(
                "Radar %dkm: render already in progress, skipping",
                self._radius_km,
            )
            return
        self._render_task = self.hass.async_create_task(
            self._render_to_cache(),
            name=f"rain_incoming_render_{self._radius_km}km",
        )

    async def _render_to_cache(self) -> None:
        """Render the composite GIF and store in self._cached_image.

        Skips the render if the frame hasn't changed since the last render.
        Wraps the render with a timeout so a slow network call never blocks
        indefinitely and explicitly falls back rather than letting image_proxy
        return a broken-image icon.
        """
        frame_path = self.coordinator.latest_frame_path
        if frame_path is None:
            _LOGGER.info(
                "Radar %dkm: coordinator has no frame data yet, skipping render",
                self._radius_km,
            )
            return

        if frame_path == self._cached_frame_path and self._cached_image is not None:
            return  # already cached this frame

        frame_paths = self.coordinator.frame_paths
        if not frame_paths:
            _LOGGER.warning(
                "Radar %dkm: coordinator has latest_frame_path but no frame_paths list",
                self._radius_km,
            )
            return

        data = self._entry.data
        lat = data.get(CONF_LATITUDE, self.hass.config.latitude)
        lon = data.get(CONF_LONGITUDE, self.hass.config.longitude)

        import zoneinfo
        ha_tz = zoneinfo.ZoneInfo(self.hass.config.time_zone)
        local_timestamps = [
            ts.astimezone(ha_tz) for ts in self.coordinator.frame_timestamps
        ] if self.coordinator.frame_timestamps else None

        session = async_get_clientsession(self.hass)
        conf_maps = self.coordinator.confidence_maps or None
        location_name = data.get(CONF_LOCATION_NAME) or None
        map_style = self.coordinator.map_style

        log_location = location_name or "default location"

        _LOGGER.debug(
            "Radar %dkm: background render starting, %d frames for %s (style=%s)",
            self._radius_km, len(frame_paths), log_location, map_style,
        )

        try:
            async with _get_render_lock(self._entry.entry_id):
                result = await asyncio.wait_for(
                    render_animated_composite(
                        lat=lat,
                        lon=lon,
                        radius_km=self._radius_km,
                        frame_paths=frame_paths,
                        frame_duration_ms=RADAR_GIF_FRAME_DURATION_MS,
                        frame_timestamps=local_timestamps,
                        tz_name=self.hass.config.time_zone,
                        confidence_maps=conf_maps,
                        location_name=location_name,
                        session=session,
                        run_in_executor=self.hass.async_add_executor_job,
                        map_style=map_style,
                        carto_api_key=self._entry.data.get(CONF_CARTO_API_KEY, ""),
                    ),
                    timeout=_RENDER_TIMEOUT_SECONDS,
                )
            self._cached_image = result
            self._cached_frame_path = frame_path
            _LOGGER.debug(
                "Radar %dkm: rendered %d bytes",
                self._radius_km, len(self._cached_image) if self._cached_image else 0,
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Radar %dkm: render timed out after %.0fs for %s; keeping previous cache or placeholder",
                self._radius_km, _RENDER_TIMEOUT_SECONDS, log_location,
            )
        except Exception:
            _LOGGER.exception(
                "Radar %dkm: render failed for %s",
                self._radius_km, log_location,
            )

    async def async_added_to_hass(self) -> None:
        """Register coordinator listener and kick off a render if data is ready.

        If the integration was reloaded and the coordinator already has frame
        data, render immediately so the cache is warm before the first request.
        """
        await super().async_added_to_hass()
        if self.coordinator.latest_frame_path is not None:
            self._schedule_render()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any in-flight render task before the entity is removed."""
        if self._render_task is not None and not self._render_task.done():
            self._render_task.cancel()
        await super().async_will_remove_from_hass()

    # ------------------------------------------------------------------
    # Image serving - return from cache only (render happens in background)
    # ------------------------------------------------------------------

    def _make_placeholder(self) -> bytes:
        """Generate a placeholder GIF with 'Waiting for radar data...' text."""
        img = Image.new("RGB", (640, 640), (30, 30, 30))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.load_default(size=18)
        except TypeError:
            font = ImageFont.load_default()
        text = "Waiting for radar data..."
        bbox = font.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((640 - tw) // 2, (640 - th) // 2), text, fill=(180, 180, 180), font=font)
        buf = BytesIO()
        img.save(buf, format="GIF")
        return buf.getvalue()

    async def async_image(self) -> bytes | None:
        """Return the pre-rendered GIF bytes from cache.

        The cache is populated by _render_to_cache() which runs as a background
        task after every coordinator update (greedy rendering). Always return
        immediately - never await the render task here. HA's image_proxy handler
        has a ~60s timeout, and awaiting a render (which itself can take up to
        55s and shares a lock with two other radius entities) easily exceeds it,
        returning HTTP 500. On cold start the placeholder is returned; the next
        request after the render completes returns the real image.
        """
        if self._cached_image is not None:
            return self._cached_image
        _LOGGER.debug(
            "Radar %dkm: serving placeholder - no cached image available "
            "(coordinator.latest_frame_path=%s, render_task=%s)",
            self._radius_km,
            self.coordinator.latest_frame_path,
            "running" if self._render_task and not self._render_task.done()
            else "done" if self._render_task else "none",
        )
        return self._make_placeholder()
