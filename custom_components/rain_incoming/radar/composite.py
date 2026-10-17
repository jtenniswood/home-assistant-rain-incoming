from __future__ import annotations

import asyncio
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

import aiohttp
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..const import RAINVIEWER_ATTRIBUTION, RAINVIEWER_ZOOM
from ..http_retry import fetch_with_retry, rate_limited_fetch
from ..providers.rainviewer import (
    PRECIP_COLOURS,
    TILE_BASE_URL,
    TILE_SIZE,
)
from .geo import lat_lon_to_tile
from .map_styles import MapStyle, MapStyleDefinition, get_style

_LOGGER = logging.getLogger(__name__)

# RainViewer Universal Blue colour scheme (scheme 2, the only available scheme)
# Reference: https://www.rainviewer.com/api/color-schemes.html
# Build a numpy array of known precipitation RGB values for vectorized filtering.
_PRECIP_RGB = np.array(
    [[r, g, b] for r, g, b, _ in PRECIP_COLOURS], dtype=np.float32
)
# Maximum L2 colour distance to accept a pixel as precipitation.
_FILTER_MAX_COLOUR_DISTANCE = 30.0

_RENDER_COLOUR_SCHEME = 2
_RAINVIEWER_TILE_BASE = TILE_BASE_URL

_CROSSHAIR_RADIUS = 8
_CROSSHAIR_LINE_LENGTH = 16
_CROSSHAIR_LINE_GAP = 12

# Module-level LRU cache for static map tiles: (style, zoom, x, y) -> (RGBA Image, timestamp)
# Bounded to prevent unbounded memory growth (each tile ~256KB).
# OrderedDict with move_to_end on access provides true LRU eviction.
# Entries expire after _MAP_CACHE_TTL_SECONDS to pick up provider tile updates.
import time as _time_module

_map_tile_cache: OrderedDict[tuple, tuple[Image.Image, float]] = OrderedDict()
_MAP_CACHE_MAX = 200
_MAP_CACHE_TTL_SECONDS = 86400  # 24 hours


def _get_cached_map_tile(cache_key: tuple) -> Image.Image | None:
    """Return a cached map tile if present and not expired, else None."""
    entry = _map_tile_cache.get(cache_key)
    if entry is None:
        return None
    tile, cached_at = entry
    if _time_module.monotonic() - cached_at > _MAP_CACHE_TTL_SECONDS:
        del _map_tile_cache[cache_key]
        return None
    _map_tile_cache.move_to_end(cache_key)
    return tile

# Module-level LRU cache for radar tiles: (frame_path, zoom, x, y, scheme) -> RGBA Image
# The cache key includes frame_path, so stale data can never be served.
# OrderedDict with move_to_end on access provides true LRU eviction.
_radar_tile_cache: OrderedDict[tuple[str, int, int, int, int], Image.Image] = OrderedDict()
_RADAR_CACHE_MAX = 500


def clear_tile_caches() -> None:
    """Clear tile caches and reset the semaphore. Called when the last entry is unloaded."""
    global _tile_semaphore
    _map_tile_cache.clear()
    _radar_tile_cache.clear()
    _tile_semaphore = None

# Semaphore to limit concurrent tile fetches (shared across map + radar)
_tile_semaphore: asyncio.Semaphore | None = None


def _get_tile_semaphore() -> asyncio.Semaphore:
    """Lazily create the tile semaphore (must be called inside a running event loop)."""
    global _tile_semaphore
    if _tile_semaphore is None:
        _tile_semaphore = asyncio.Semaphore(10)
    return _tile_semaphore


def _evict_radar_cache_if_full() -> None:
    """Evict least-recently-used radar tiles when the cache exceeds the size limit."""
    while len(_radar_tile_cache) > _RADAR_CACHE_MAX:
        _radar_tile_cache.popitem(last=False)  # pop from LRU end (front)


def _evict_map_cache_if_full() -> None:
    """Evict least-recently-used map tiles when the cache exceeds the size limit."""
    while len(_map_tile_cache) > _MAP_CACHE_MAX:
        _map_tile_cache.popitem(last=False)  # pop from LRU end (front)


def km_per_pixel(lat: float, zoom: int) -> float:
    return (40075.0 / (256 * 2**zoom)) * math.cos(math.radians(lat))


def calculate_map_zoom(lat: float, radius_km: int, output_size: int) -> int:
    best_zoom = 1
    best_diff = float("inf")
    for z in range(1, 19):
        kpp = km_per_pixel(lat, z)
        if kpp <= 0:
            continue
        radius_pixels = radius_km / kpp
        diameter_pixels = 2 * radius_pixels
        diff = abs(diameter_pixels - output_size)
        if diff < best_diff:
            best_diff = diff
            best_zoom = z
    return best_zoom


def _lat_lon_to_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Return global pixel coordinates for lat/lon at given zoom."""
    n = 2**zoom
    px = (lon + 180.0) / 360.0 * n * 256
    lat_r = math.radians(lat)
    py = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n * 256
    return px, py


def filter_precipitation_pixels(rgba_array: np.ndarray) -> np.ndarray:
    """Keep only pixels matching the documented RainViewer precipitation colours.

    RainViewer tiles contain two kinds of non-transparent pixels:
    - Precipitation colours (blues, cyans, yellows, reds, magentas) from the
      documented Universal Blue scheme 2 colour table.
    - Land-mask / ground-clutter colours (khaki/beige, e.g. 170,158,121) that
      are NOT in the documented scheme.

    We match each pixel's RGB against the known precipitation colours using L2
    distance. Pixels that don't match any known colour (distance > threshold)
    are made fully transparent.

    Uses numpy vectorized operations - no Python loops over pixels.
    """
    result = rgba_array.copy()

    # Transparent pixels stay transparent - skip them from colour matching
    alpha_mask = rgba_array[:, :, 3] > 10

    if not alpha_mask.any():
        # No opaque pixels to colour-match, but still zero out low-alpha pixels
        result[:, :, 3] = 0
        return result

    # Extract RGB as float for distance calculation
    rgb = rgba_array[:, :, :3].astype(np.float32)

    # Compute L2 distance from each pixel to each known precipitation colour
    # rgb: (H, W, 3), _PRECIP_RGB: (N, 3) -> diff: (H, W, N, 3)
    diff = rgb[:, :, np.newaxis, :] - _PRECIP_RGB[np.newaxis, np.newaxis, :, :]
    distances = np.sqrt((diff ** 2).sum(axis=-1))  # (H, W, N)
    best_dist = distances.min(axis=-1)  # (H, W)

    # Pixels that are non-transparent but don't match any precipitation colour
    non_precip = alpha_mask & (best_dist > _FILTER_MAX_COLOUR_DISTANCE)
    result[non_precip, 3] = 0

    # Also zero out truly transparent pixels
    result[~alpha_mask, 3] = 0

    return result


def draw_crosshair(img: Image.Image, cx: int, cy: int) -> None:
    """Draw a red crosshair with white outline at (cx, cy)."""
    draw = ImageDraw.Draw(img)
    r = _CROSSHAIR_RADIUS
    gap = _CROSSHAIR_LINE_GAP
    length = _CROSSHAIR_LINE_LENGTH

    outline_color = (255, 255, 255, 160)
    fill_color = (200, 50, 50, 180)

    # White outline circle
    draw.ellipse(
        [cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1],
        outline=outline_color, width=1,
    )
    # Red circle
    draw.ellipse(
        [cx - r, cy - r, cx + r, cy + r],
        outline=fill_color, width=2,
    )

    # Crosshair lines with white outline then red fill
    for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
        x0 = cx + dx * gap
        y0 = cy + dy * gap
        x1 = cx + dx * (gap + length)
        y1 = cy + dy * (gap + length)
        draw.line([(x0, y0), (x1, y1)], fill=outline_color, width=3)
        draw.line([(x0, y0), (x1, y1)], fill=fill_color, width=1)


def draw_range_rings(
    img: Image.Image, cx: int, cy: int, full_radius_px: int, radius_km: int,
    is_dark: bool = False,
) -> None:
    """Draw subtle range rings at half-radius and full-radius with distance labels."""
    draw = ImageDraw.Draw(img)
    ring_color = (255, 255, 255, 80)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()

    for factor, km in ((0.5, radius_km // 2), (1.0, radius_km)):
        r = int(full_radius_px * factor)
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            outline=ring_color, width=1,
        )
        label = f"{km}km"
        bbox = font.getbbox(label)
        _, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        lx = cx + 4
        ly = cy - r - th - 2
        _draw_text_with_outline(draw, lx, ly, label, font, is_dark=is_dark)


async def _fetch_tile(session: aiohttp.ClientSession, url: str) -> Image.Image:
    # Hold the semaphore for the full connection lifecycle: request + body read.
    # Previously the semaphore was released when fetch_with_retry returned the
    # response object (headers received), allowing more simultaneous connections
    # than the limit. Holding through resp.read() closes that gap.
    async with _get_tile_semaphore():
        resp = await fetch_with_retry(
            session, url, max_retries=2, timeout_total=15.0
        )
        data = await asyncio.wait_for(resp.read(), timeout=10)
    return Image.open(BytesIO(data)).convert("RGBA")


async def _fetch_map_tile(
    session: aiohttp.ClientSession,
    zoom: int,
    tx: int,
    ty: int,
    style_def: MapStyleDefinition | None = None,
) -> Image.Image | None:
    """Fetch a map tile using the given style, with module-level cache for hits.

    style_def defaults to VOYAGER if not provided (preserves previous behaviour).
    """
    if style_def is None:
        style_def = get_style(MapStyle.VOYAGER)

    # Cache key includes style so different styles don't share stale tiles.
    cache_key = (style_def.style, zoom, tx, ty)
    if style_def.credential_cache_key:
        cache_key += (style_def.credential_cache_key,)
    cached = _get_cached_map_tile(cache_key)
    if cached is not None:
        return cached

    # Gate cache-miss fetches through the shared semaphore so map tiles count
    # against the same concurrency limit as radar tiles.
    async with _get_tile_semaphore():
        tile = await style_def.fetch_tile(session, zoom, tx, ty)
    if tile is None:
        return None

    # Voyager benefits from a saturation boost for richer land/water colours.
    # Other styles manage their own post-processing (e.g. OSM Dark applies its
    # own transform inside _fetch_osm_dark).
    if style_def.style == MapStyle.VOYAGER:
        from PIL import ImageEnhance
        tile = ImageEnhance.Color(tile).enhance(1.8)

    _evict_map_cache_if_full()
    _map_tile_cache[cache_key] = (tile, _time_module.monotonic())  # type: ignore[index]
    return tile


def _truncate_location(name: str, max_len: int = 16) -> str:
    """Truncate a location name to max_len chars, adding ellipsis if needed."""
    if len(name) > max_len:
        return name[:max_len - 1] + "\u2026"
    return name


def build_frame_label(
    timestamp: datetime | None,
    tz_name: str | None,
    radius_km: int,
    location_name: str | None = None,
) -> str:
    """Build the BOM-style label string for a GIF frame.

    Format: "Location  DD/MM/YY  HH:MM TZ  Rangekm"
    Location is omitted if not set.
    Location names > 16 chars are truncated to 15 chars + ellipsis as a
    render-time safety net (config validation enforces MAX_LOCATION_NAME_CHARS).
    """
    parts = []
    if location_name:
        parts.append(_truncate_location(location_name))
    if timestamp is not None:
        tz_label = timestamp.strftime("%Z") or tz_name or "UTC"
        parts.append(timestamp.strftime("%d/%m/%y"))
        parts.append(timestamp.strftime(f"%H:%M {tz_label}"))
    parts.append(f"{radius_km}km")
    return "  ".join(parts)


def _draw_frame_label(
    img: Image.Image,
    timestamp: datetime | None,
    tz_name: str | None,
    radius_km: int,
    location_name: str | None = None,
    *,
    frame_index: int | None = None,
    frame_count: int | None = None,
    is_dark: bool = False,
) -> None:
    """Draw header labels across the top of a frame in three positions.

    - Top-left: location name (e.g. "Sydney")
    - Top-centre: date/time (e.g. "10/04/26  20:40 UTC"), anchor='mt'
    - Top-right: range + frame counter (e.g. "128km  3/13"), anchor='rt'
    """
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=16)
    except TypeError:
        font = ImageFont.load_default()

    padding = 8

    # --- Top-left: location name ---
    if location_name:
        _draw_text_with_outline(draw, padding, padding, _truncate_location(location_name), font, is_dark=is_dark)

    # --- Top-centre: date/time ---
    if timestamp is not None:
        tz_label = timestamp.strftime("%Z") or tz_name or "UTC"
        datetime_text = f"{timestamp.strftime('%d/%m/%y')}  {timestamp.strftime(f'%H:%M {tz_label}')}"
        cx = img.width // 2
        _draw_text_with_outline_anchored(draw, cx, padding, datetime_text, font, anchor="mt", is_dark=is_dark)

    # --- Top-right: range + frame counter ---
    range_text = f"{radius_km}km"
    if frame_index is not None and frame_count is not None:
        range_text = f"{range_text}  {frame_index}/{frame_count}"
    rx = img.width - padding
    _draw_text_with_outline_anchored(draw, rx, padding, range_text, font, anchor="rt", is_dark=is_dark)


def _draw_attribution(img: Image.Image, style_def: MapStyleDefinition) -> None:
    """Draw the RainViewer + map provider attribution along the full bottom of the composite.

    RainViewer comes first per their API terms (rainviewer.com/api.html).
    Frame label is now at top-left, so the entire bottom edge is available.
    14pt fits the full string (569px for ESRI) comfortably in a 640px image (624px available).
    """
    text = f"{RAINVIEWER_ATTRIBUTION} | {style_def.attribution}"
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()

    padding = 8
    text_bbox = font.getbbox(text)
    text_h = text_bbox[3] - text_bbox[1]

    lx = padding
    ly = img.height - text_h - padding

    _draw_text_with_outline(draw, lx, ly, text, font, is_dark=style_def.is_dark)


def _text_colours(is_dark: bool) -> tuple[tuple, tuple]:
    """Return (outline_colour, fill_colour) for text on light or dark backgrounds."""
    if is_dark:
        return (0, 0, 0, 200), (255, 255, 255, 230)
    return (255, 255, 255, 220), (0, 0, 0, 230)


def _draw_text_with_outline(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font,
    is_dark: bool = False,
) -> None:
    """Draw text with a contrasting outline for readability on any background.

    On light maps: black text, white outline.
    On dark maps: white text, black outline.
    """
    outline, fill = _text_colours(is_dark)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                draw.text((x + dx, y + dy), text, fill=outline, font=font)
    draw.text((x, y), text, fill=fill, font=font)


def _draw_text_with_outline_anchored(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font,
    anchor: str = "la",
    is_dark: bool = False,
) -> None:
    """Draw text with a contrasting outline using a Pillow text anchor.

    anchor='mt' for top-centre, anchor='rt' for top-right, anchor='la' for top-left.
    """
    outline, fill = _text_colours(is_dark)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                draw.text(
                    (x + dx, y + dy), text,
                    fill=outline, font=font, anchor=anchor,
                )
    draw.text((x, y), text, fill=fill, font=font, anchor=anchor)


@dataclass(frozen=True)
class FrameRenderContext:
    """Shared rendering context for compositing frames.

    Groups the parameters that are constant across all frames in an animation,
    reducing _composite_single_frame from 14 params to 5.
    """
    lat: float
    lon: float
    map_zoom: int
    radius_km: int
    output_size: int
    tz_name: str | None = None
    location_name: str | None = None
    style_def: MapStyleDefinition | None = None


def _composite_single_frame(
    map_crop: Image.Image,
    radar_resized: Image.Image,
    ctx: FrameRenderContext,
    timestamp: datetime | None = None,
    confidence_map: np.ndarray | None = None,
    frame_index: int | None = None,
    frame_count: int | None = None,
) -> Image.Image:
    """CPU-bound rendering: composite map + radar, draw overlays. Returns RGBA Image.

    When confidence_map is provided (float32, 0-1, same size as radar_resized),
    it's applied as an alpha multiplier to reduce clutter opacity.
    """
    if confidence_map is not None:
        radar_arr = np.array(radar_resized)
        # Binary detection mask: pixels with strong radar returns render at full
        # opacity regardless of QC confidence. The threshold is applied to raw
        # luminance so that real precipitation (bright pixels in the radar tile)
        # is never dimmed by a low confidence score. Confidence only dims pixels
        # that are already faint/borderline - i.e. where luminance alone is below
        # the threshold.
        _INTENSITY_THRESHOLD = 0.1
        _DIM_FACTOR = 0.25
        r, g, b = (
            radar_arr[:, :, 0].astype(np.float32),
            radar_arr[:, :, 1].astype(np.float32),
            radar_arr[:, :, 2].astype(np.float32),
        )
        luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        alpha_multiplier = np.where(
            luminance >= _INTENSITY_THRESHOLD, 1.0, _DIM_FACTOR
        )
        radar_arr[:, :, 3] = (
            radar_arr[:, :, 3].astype(np.float32) * alpha_multiplier
        ).astype(np.uint8)
        radar_resized = Image.fromarray(radar_arr)

    composite = Image.alpha_composite(map_crop.convert("RGBA"), radar_resized)

    kpp = km_per_pixel(ctx.lat, ctx.map_zoom)
    radius_px = int(ctx.radius_km / kpp) if kpp > 0 else ctx.output_size // 2

    cx, cy = ctx.output_size // 2, ctx.output_size // 2
    style_def = ctx.style_def or get_style(MapStyle.VOYAGER)

    draw_range_rings(composite, cx, cy, radius_px, ctx.radius_km, is_dark=style_def.is_dark)
    draw_crosshair(composite, cx, cy)

    _draw_frame_label(
        composite, timestamp, ctx.tz_name, ctx.radius_km, ctx.location_name,
        frame_index=frame_index, frame_count=frame_count,
        is_dark=style_def.is_dark,
    )

    _draw_attribution(composite, style_def)

    return composite


def _render_sync(
    map_crop: Image.Image,
    radar_resized: Image.Image,
    lat: float,
    lon: float,
    map_zoom: int,
    radius_km: int,
    output_size: int,
    style_def: MapStyleDefinition | None = None,
) -> bytes:
    """CPU-bound rendering: composite, draw overlays, export PNG."""
    ctx = FrameRenderContext(
        lat=lat, lon=lon, map_zoom=map_zoom, radius_km=radius_km,
        output_size=output_size, style_def=style_def,
    )
    composite = _composite_single_frame(map_crop, radar_resized, ctx)
    buf = BytesIO()
    composite.save(buf, format="PNG")
    return buf.getvalue()


def _render_gif_sync(
    frames: list[Image.Image],
    frame_duration_ms: int,
) -> bytes:
    """CPU-bound: assemble RGBA frames into an animated GIF. Returns GIF bytes."""
    # Quantize all frames to the FIRST frame's palette so colors (crosshair,
    # range rings, labels) stay consistent across the animation.
    first_quantized = frames[0].convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT)

    quantized_frames = []
    for f in frames:
        rgb = f.convert("RGB")
        q = rgb.quantize(palette=first_quantized, dither=Image.Dither.FLOYDSTEINBERG)
        quantized_frames.append(q)

    buf = BytesIO()
    if len(quantized_frames) == 1:
        quantized_frames[0].save(buf, format="GIF")
    else:
        durations = [frame_duration_ms] * len(quantized_frames)
        durations[-1] = frame_duration_ms * 4  # hold on last frame
        quantized_frames[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=quantized_frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
        )
    return buf.getvalue()


class _ViewportParams:
    """Pre-computed viewport geometry shared across frames."""

    __slots__ = (
        "map_zoom", "centre_px", "centre_py", "half_size",
        "tile_x_min", "tile_x_max", "tile_y_min", "tile_y_max",
        "radar_vp_left", "radar_vp_right", "radar_vp_top", "radar_vp_bottom",
        "radar_tx_min", "radar_tx_max", "radar_ty_min", "radar_ty_max",
    )

    def __init__(self, lat: float, lon: float, radius_km: int, output_size: int) -> None:
        self.map_zoom = calculate_map_zoom(lat, radius_km, output_size)
        self.centre_px, self.centre_py = _lat_lon_to_pixel(lat, lon, self.map_zoom)
        self.half_size = output_size / 2

        self.tile_x_min = int((self.centre_px - self.half_size) // 256)
        self.tile_x_max = int((self.centre_px + self.half_size) // 256)
        self.tile_y_min = int((self.centre_py - self.half_size) // 256)
        self.tile_y_max = int((self.centre_py + self.half_size) // 256)

        radar_zoom = RAINVIEWER_ZOOM
        map_n = 2 ** self.map_zoom
        radar_n = 2 ** radar_zoom
        scale = radar_n / map_n

        vp_left = self.centre_px - self.half_size
        vp_right = self.centre_px + self.half_size
        vp_top = self.centre_py - self.half_size
        vp_bottom = self.centre_py + self.half_size

        self.radar_vp_left = vp_left * scale
        self.radar_vp_right = vp_right * scale
        self.radar_vp_top = vp_top * scale
        self.radar_vp_bottom = vp_bottom * scale

        self.radar_tx_min = int(self.radar_vp_left // 256)
        self.radar_tx_max = int(self.radar_vp_right // 256)
        self.radar_ty_min = int(self.radar_vp_top // 256)
        self.radar_ty_max = int(self.radar_vp_bottom // 256)


async def _fetch_map_crop(
    session: aiohttp.ClientSession,
    vp: _ViewportParams,
    output_size: int,
    style_def: MapStyleDefinition | None = None,
) -> Image.Image:
    """Fetch and stitch map tiles (in parallel), then crop to viewport."""
    if style_def is None:
        style_def = get_style(MapStyle.VOYAGER)

    coords = [
        (tx, ty)
        for ty in range(vp.tile_y_min, vp.tile_y_max + 1)
        for tx in range(vp.tile_x_min, vp.tile_x_max + 1)
    ]

    async def _fetch_one(tx: int, ty: int):
        try:
            return tx, ty, await _fetch_map_tile(session, vp.map_zoom, tx, ty, style_def)
        except aiohttp.ClientResponseError as e:
            _LOGGER.warning(
                "Map tile fetch failed: HTTP %d for z=%d x=%d y=%d",
                e.status, vp.map_zoom, tx, ty,
            )
            return tx, ty, None
        except Exception as e:
            _LOGGER.warning(
                "Map tile fetch failed: %s: %s (z=%d x=%d y=%d)",
                type(e).__name__, e, vp.map_zoom, tx, ty,
            )
            return tx, ty, None

    results = await asyncio.gather(*[_fetch_one(tx, ty) for tx, ty in coords])

    canvas_w = (vp.tile_x_max - vp.tile_x_min + 1) * 256
    canvas_h = (vp.tile_y_max - vp.tile_y_min + 1) * 256
    map_canvas = Image.new("RGBA", (canvas_w, canvas_h))

    for tx, ty, tile in results:
        if tile is not None:
            map_canvas.paste(tile, ((tx - vp.tile_x_min) * 256, (ty - vp.tile_y_min) * 256))

    crop_x = int(vp.centre_px - vp.half_size - vp.tile_x_min * 256)
    crop_y = int(vp.centre_py - vp.half_size - vp.tile_y_min * 256)
    return map_canvas.crop((crop_x, crop_y, crop_x + output_size, crop_y + output_size))


async def _fetch_radar_overlay(
    session: aiohttp.ClientSession,
    vp: _ViewportParams,
    frame_path: str,
    output_size: int,
) -> Image.Image:
    """Fetch radar tiles for one frame (in parallel), filter, crop, and resize to viewport."""
    radar_zoom = RAINVIEWER_ZOOM

    coords = [
        (tx, ty)
        for ty in range(vp.radar_ty_min, vp.radar_ty_max + 1)
        for tx in range(vp.radar_tx_min, vp.radar_tx_max + 1)
    ]

    _LOGGER.debug(
        "radar overlay %s: %d tiles needed (%d,%d)-(%d,%d)",
        frame_path[-8:], len(coords),
        vp.radar_tx_min, vp.radar_ty_min, vp.radar_tx_max, vp.radar_ty_max,
    )

    # Evict old entries if cache is too large
    _evict_radar_cache_if_full()

    import time as _time

    async def _fetch_one(tx: int, ty: int):
        cache_key = (frame_path, radar_zoom, tx, ty, _RENDER_COLOUR_SCHEME)
        cached = _radar_tile_cache.get(cache_key)
        if cached is not None:
            _radar_tile_cache.move_to_end(cache_key)  # LRU touch
            return tx, ty, cached

        url = (
            f"{_RAINVIEWER_TILE_BASE}{frame_path}"
            f"/{TILE_SIZE}/{radar_zoom}/{tx}/{ty}/{_RENDER_COLOUR_SCHEME}/0.png"
        )
        t0 = _time.monotonic()
        try:
            tile = await _fetch_tile(session, url)
            elapsed = _time.monotonic() - t0
            if elapsed > 5:
                # DEBUG not WARNING: at WARNING this floods the HA system log
                # (~36 entries per 10-minute poll) and triggers HA's internal log
                # throttler. Slow-but-successful fetches are not actionable for users.
                _LOGGER.debug(
                    "Slow tile fetch: %.1fs for z=%d x=%d y=%d (%s)",
                    elapsed, radar_zoom, tx, ty, frame_path[-8:],
                )
            _radar_tile_cache[cache_key] = tile
            return tx, ty, tile
        except aiohttp.ClientResponseError as e:
            _LOGGER.warning(
                "Radar tile fetch failed: HTTP %d for z=%d x=%d y=%d (%s)",
                e.status, radar_zoom, tx, ty, frame_path,
            )
            return tx, ty, None
        except Exception as e:
            _LOGGER.warning(
                "Radar tile fetch failed: %s: %s (z=%d x=%d y=%d)",
                type(e).__name__, e, radar_zoom, tx, ty,
            )
            return tx, ty, None

    results = await asyncio.gather(*[_fetch_one(tx, ty) for tx, ty in coords])

    radar_canvas_w = (vp.radar_tx_max - vp.radar_tx_min + 1) * 256
    radar_canvas_h = (vp.radar_ty_max - vp.radar_ty_min + 1) * 256
    radar_canvas = Image.new("RGBA", (radar_canvas_w, radar_canvas_h), (0, 0, 0, 0))

    for tx, ty, tile in results:
        if tile is not None:
            radar_canvas.paste(tile, ((tx - vp.radar_tx_min) * 256, (ty - vp.radar_ty_min) * 256))

    radar_arr = np.array(radar_canvas)
    radar_arr = filter_precipitation_pixels(radar_arr)

    radar_filtered = Image.fromarray(radar_arr)

    radar_crop_x = int(vp.radar_vp_left - vp.radar_tx_min * 256)
    radar_crop_y = int(vp.radar_vp_top - vp.radar_ty_min * 256)
    radar_crop_w = int(vp.radar_vp_right - vp.radar_vp_left)
    radar_crop_h = int(vp.radar_vp_bottom - vp.radar_vp_top)

    radar_crop = radar_filtered.crop((
        radar_crop_x, radar_crop_y,
        radar_crop_x + radar_crop_w, radar_crop_y + radar_crop_h,
    ))

    return radar_crop.resize((output_size, output_size), Image.BILINEAR)


async def render_composite(
    lat: float,
    lon: float,
    radius_km: int,
    frame_path: str,
    output_size: int = 640,
    *,
    session: aiohttp.ClientSession,
    run_in_executor: object = None,
    map_style: MapStyle | str = MapStyle.VOYAGER,
    carto_api_key: str = "",
) -> bytes:
    """Render a radar composite image as PNG bytes.

    Parameters
    ----------
    session: an aiohttp.ClientSession for fetching tiles.
    run_in_executor: optional callable (e.g. hass.async_add_executor_job)
        to offload CPU-bound rendering. If None, rendering runs inline.
    map_style: base map provider. Defaults to VOYAGER.
    """
    style_def = get_style(map_style, carto_api_key=carto_api_key)
    vp = _ViewportParams(lat, lon, radius_km, output_size)
    map_crop = await _fetch_map_crop(session, vp, output_size, style_def)
    radar_resized = await _fetch_radar_overlay(session, vp, frame_path, output_size)

    if run_in_executor is not None:
        return await run_in_executor(
            _render_sync, map_crop, radar_resized, lat, lon, vp.map_zoom, radius_km, output_size,
            style_def,
        )

    return _render_sync(map_crop, radar_resized, lat, lon, vp.map_zoom, radius_km, output_size, style_def)


# --- Public composable API ---
# These functions expose the rendering pipeline as independent building blocks.
# Production code uses render_animated_composite() which calls all of them.
# Tests can call them individually for isolated assertions.


async def fetch_map_crop(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    radius_km: int,
    output_size: int = 640,
    map_style: MapStyle | str = MapStyle.VOYAGER,
    carto_api_key: str = "",
) -> Image.Image:
    """Fetch map tiles for a location and radius. Returns a cropped PIL image.

    map_style selects the base map provider. Defaults to VOYAGER (CARTO) to
    preserve existing production behaviour. Pass any MapStyle value or its
    string equivalent (e.g. "osm_dark") to change the map style.
    """
    style_def = get_style(map_style, carto_api_key=carto_api_key)
    vp = _ViewportParams(lat, lon, radius_km, output_size)
    return await _fetch_map_crop(session, vp, output_size, style_def)


async def fetch_radar_overlays(
    session: aiohttp.ClientSession,
    lat: float,
    lon: float,
    radius_km: int,
    frame_paths: list[str],
    output_size: int = 640,
) -> list[Image.Image]:
    """Fetch radar tiles for each frame path. Returns a list of RGBA PIL images."""
    vp = _ViewportParams(lat, lon, radius_km, output_size)
    return list(await asyncio.gather(*[
        _fetch_radar_overlay(session, vp, fp, output_size) for fp in frame_paths
    ]))


def composite_frames(
    background: Image.Image,
    radar_overlays: list[Image.Image],
    lat: float,
    lon: float,
    radius_km: int,
    output_size: int = 640,
    *,
    frame_timestamps: list[datetime] | None = None,
    tz_name: str | None = None,
    confidence_maps: list[np.ndarray] | None = None,
    location_name: str | None = None,
    map_style: MapStyle | str = MapStyle.VOYAGER,
) -> list[Image.Image]:
    """Composite radar overlays over a background image. Returns a list of frames."""
    style_def = get_style(map_style)
    vp = _ViewportParams(lat, lon, radius_km, output_size)
    ctx = FrameRenderContext(
        lat=lat, lon=lon, map_zoom=vp.map_zoom, radius_km=radius_km,
        output_size=output_size, tz_name=tz_name,
        location_name=location_name, style_def=style_def,
    )
    timestamps = frame_timestamps or [None] * len(radar_overlays)
    total_frames = len(radar_overlays)
    frames: list[Image.Image] = []
    for i, radar_resized in enumerate(radar_overlays):
        ts = timestamps[i] if i < len(timestamps) else None
        conf_map = confidence_maps[i] if confidence_maps and i < len(confidence_maps) else None
        frame_img = _composite_single_frame(
            background, radar_resized, ctx,
            timestamp=ts, confidence_map=conf_map,
            frame_index=i + 1, frame_count=total_frames,
        )
        frames.append(frame_img)
    return frames


def render_gif(
    frames: list[Image.Image],
    frame_duration_ms: int = 500,
) -> bytes:
    """Encode PIL frames to animated GIF bytes."""
    if not frames:
        blank = Image.new("RGB", (100, 100), (0, 0, 0))
        buf = BytesIO()
        blank.save(buf, format="GIF")
        return buf.getvalue()
    return _render_gif_sync(frames, frame_duration_ms)


# --- High-level convenience function (used by image.py) ---


async def render_animated_composite(
    lat: float,
    lon: float,
    radius_km: int,
    frame_paths: list[str],
    output_size: int = 640,
    frame_duration_ms: int = 500,
    *,
    frame_timestamps: list[datetime] | None = None,
    tz_name: str | None = None,
    confidence_maps: list[np.ndarray] | None = None,
    location_name: str | None = None,
    session: aiohttp.ClientSession,
    run_in_executor: object = None,
    map_style: MapStyle | str = MapStyle.VOYAGER,
    carto_api_key: str = "",
) -> bytes:
    """Render an animated GIF compositing multiple radar frames over a static map.

    Convenience function that calls fetch_map_crop, fetch_radar_overlays,
    composite_frames, and render_gif in sequence.
    """
    import time as _time
    t0 = _time.monotonic()

    map_crop = await fetch_map_crop(
        session, lat, lon, radius_km, output_size, map_style,
        carto_api_key=carto_api_key,
    )
    t1 = _time.monotonic()
    _LOGGER.debug("render %dkm: map tiles fetched in %.1fs", radius_km, t1 - t0)

    radar_overlays = await fetch_radar_overlays(
        session, lat, lon, radius_km, frame_paths, output_size
    )
    t2 = _time.monotonic()
    _LOGGER.debug("render %dkm: %d radar overlays fetched in %.1fs", radius_km, len(radar_overlays), t2 - t1)

    import functools
    _composite = functools.partial(
        composite_frames,
        background=map_crop,
        radar_overlays=radar_overlays,
        lat=lat, lon=lon, radius_km=radius_km,
        output_size=output_size,
        frame_timestamps=frame_timestamps,
        tz_name=tz_name,
        confidence_maps=confidence_maps,
        location_name=location_name,
        map_style=map_style,
    )
    if run_in_executor is not None:
        frames = await run_in_executor(_composite)
    else:
        frames = _composite()
    t3 = _time.monotonic()
    _LOGGER.debug("render %dkm: %d frames composited in %.1fs", radius_km, len(frames), t3 - t2)

    if not frames:
        # Shouldn't happen, but return a blank GIF if no frames
        blank = Image.new("RGB", (output_size, output_size), (0, 0, 0))
        buf = BytesIO()
        blank.save(buf, format="GIF")
        return buf.getvalue()

    if run_in_executor is not None:
        return await run_in_executor(_render_gif_sync, frames, frame_duration_ms)

    return _render_gif_sync(frames, frame_duration_ms)
