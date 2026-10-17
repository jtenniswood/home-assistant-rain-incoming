"""Selectable base map styles for the radar composite.

Each style encapsulates the tile fetching + any post-processing needed to
produce a finished 256x256 RGBA base map tile for a given (zoom, x, y). The
renderer delegates tile fetching to the style's `fetch_tile` function so new
styles can be added without changing the compositing pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from functools import partial
from hashlib import sha256
from enum import StrEnum
from io import BytesIO
from typing import Awaitable, Callable

import aiohttp
import numpy as np
from PIL import Image, ImageEnhance

from ..http_retry import fetch_with_retry

_LOGGER = logging.getLogger(__name__)

# Saturation multiplier applied to OSM tiles before the HSV V-invert + feature lift.
# Lower values give a more muted "dark mode" aesthetic closer to the original
# HSV V-invert experiment the user approved. Tune here if you want the OSM
# dark variant more/less saturated.
_OSM_DARK_SATURATION_FACTOR = 0.75

_TILE_SIZE = 256

_USER_AGENT = "home-assistant rain_incoming"


class MapStyle(StrEnum):
    VOYAGER = "voyager"
    OSM_STANDARD = "osm_standard"
    OSM_DARK = "osm_dark"
    ESRI_IMAGERY = "esri_imagery"
    DARK_MATTER = "dark_matter"


FetchTileFn = Callable[[aiohttp.ClientSession, int, int, int], Awaitable[Image.Image | None]]


@dataclass(frozen=True)
class MapStyleDefinition:
    style: MapStyle
    display_name: str
    attribution: str
    fetch_tile: FetchTileFn
    is_dark: bool = False
    credential_cache_key: str = field(default="", repr=False)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

async def _fetch_png(
    session: aiohttp.ClientSession, url: str, *, api_key: str = ""
) -> Image.Image | None:
    """Fetch a PNG tile URL and return it as an RGBA PIL Image, or None on failure.

    Uses fetch_with_retry for 429/5xx retry with exponential backoff.
    404 errors are silently returned as None (expected for ESRI overlay tiles at
    some zoom levels). Other 4xx errors are logged at WARNING and return None.
    Network/timeout errors are also logged at WARNING and return None.
    """
    try:
        resp = await fetch_with_retry(
            session, url, headers={"User-Agent": _USER_AGENT},
            **({"params": {"key": api_key}} if api_key else {}),
        )
        data = await resp.read()
        return Image.open(BytesIO(data)).convert("RGBA")
    except aiohttp.ClientResponseError as exc:
        if exc.status == 404:
            return None  # overlay tile doesn't exist at this zoom - expected, don't warn
        _LOGGER.warning("Map tile fetch failed: HTTP %d for %s", exc.status, url)
        return None
    except Exception as exc:
        _LOGGER.warning(
            "Map tile fetch error: %s for %s", type(exc).__name__, url
        )
        return None


def _apply_osm_dark_transform(img: Image.Image) -> Image.Image:
    """HSV V-invert + feature lift + saturation reduction for the OSM dark variant.

    Step 1: reduce saturation via _OSM_DARK_SATURATION_FACTOR to tone down
            overly vivid bushland greens compared to a plain HSV invert.
    Step 2: invert the V channel so bright areas go dark and vice versa.
    Step 3: feature lift - make dark features (roads, labels) bright so they
            remain visible on the dark background.
    """
    desaturated = ImageEnhance.Color(img).enhance(_OSM_DARK_SATURATION_FACTOR)
    hsv_arr = np.array(desaturated.convert("HSV")).astype(np.int16)
    hsv_arr[:, :, 2] = 255 - hsv_arr[:, :, 2]
    luma = np.array(desaturated.convert("L")).astype(np.int16)
    feature_lift = 255 - luma
    hsv_arr[:, :, 2] = np.maximum(hsv_arr[:, :, 2], feature_lift)
    hsv_arr = hsv_arr.clip(0, 255).astype(np.uint8)
    h, w = hsv_arr.shape[:2]
    return Image.frombytes("HSV", (w, h), hsv_arr.tobytes()).convert("RGB").convert("RGBA")


# ---------------------------------------------------------------------------
# Per-style fetch functions
# ---------------------------------------------------------------------------

async def _fetch_voyager(
    session: aiohttp.ClientSession, z: int, x: int, y: int, *, api_key: str = ""
) -> Image.Image | None:
    return await _fetch_png(
        session, f"https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
        api_key=api_key,
    )


async def _fetch_osm_standard(
    session: aiohttp.ClientSession, z: int, x: int, y: int
) -> Image.Image | None:
    return await _fetch_png(session, f"https://tile.openstreetmap.org/{z}/{x}/{y}.png")


async def _fetch_osm_dark(
    session: aiohttp.ClientSession, z: int, x: int, y: int
) -> Image.Image | None:
    base = await _fetch_osm_standard(session, z, x, y)
    return _apply_osm_dark_transform(base) if base is not None else None


async def _fetch_dark_matter(
    session: aiohttp.ClientSession, z: int, x: int, y: int, *, api_key: str = ""
) -> Image.Image | None:
    return await _fetch_png(
        session, f"https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        api_key=api_key,
    )


async def _fetch_esri_imagery(
    session: aiohttp.ClientSession, z: int, x: int, y: int
) -> Image.Image | None:
    # Note: ESRI tile URL pattern uses {z}/{y}/{x} - y before x (unlike OSM).
    base = await _fetch_png(
        session,
        f"https://server.arcgisonline.com/ArcGIS/rest/services"
        f"/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    )
    if base is None:
        return None

    # Overlay layers are nice-to-have: composite what we can, fall back gracefully.
    roads = await _fetch_png(
        session,
        f"https://server.arcgisonline.com/ArcGIS/rest/services"
        f"/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}",
    )
    labels = await _fetch_png(
        session,
        f"https://server.arcgisonline.com/ArcGIS/rest/services"
        f"/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
    )

    result = base.copy()
    if roads is not None:
        result.alpha_composite(roads)
    if labels is not None:
        result.alpha_composite(labels)
    return result


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_STYLES: dict[MapStyle, MapStyleDefinition] = {
    MapStyle.VOYAGER: MapStyleDefinition(
        style=MapStyle.VOYAGER,
        display_name="CARTO Voyager",
        attribution="© CARTO, © OpenStreetMap contributors",
        fetch_tile=_fetch_voyager,
    ),
    MapStyle.OSM_STANDARD: MapStyleDefinition(
        style=MapStyle.OSM_STANDARD,
        display_name="OpenStreetMap",
        attribution="© OpenStreetMap contributors",
        fetch_tile=_fetch_osm_standard,
    ),
    MapStyle.OSM_DARK: MapStyleDefinition(
        style=MapStyle.OSM_DARK,
        display_name="OpenStreetMap Dark",
        attribution="© OpenStreetMap contributors",
        fetch_tile=_fetch_osm_dark,
        is_dark=True,
    ),
    MapStyle.ESRI_IMAGERY: MapStyleDefinition(
        style=MapStyle.ESRI_IMAGERY,
        display_name="ESRI World Imagery",
        attribution="Sources: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
        fetch_tile=_fetch_esri_imagery,
        is_dark=True,
    ),
    MapStyle.DARK_MATTER: MapStyleDefinition(
        style=MapStyle.DARK_MATTER,
        display_name="Dark Matter",
        attribution="© CARTO, © OpenStreetMap contributors",
        fetch_tile=_fetch_dark_matter,
        is_dark=True,
    ),
}


def get_style(style: MapStyle | str, *, carto_api_key: str = "") -> MapStyleDefinition:
    """Lookup a MapStyleDefinition by enum or string value.

    Raises ValueError for unrecognised style names.
    """
    if isinstance(style, str) and not isinstance(style, MapStyle):
        try:
            style = MapStyle(style)
        except ValueError as exc:
            raise ValueError(
                f"Unknown map style: {style!r}. Valid: {[s.value for s in MapStyle]}"
            ) from exc
    if style not in _STYLES:
        raise ValueError(f"Unknown map style: {style!r}")
    definition = _STYLES[style]
    if carto_api_key and style in (MapStyle.VOYAGER, MapStyle.DARK_MATTER):
        return replace(
            definition,
            fetch_tile=partial(definition.fetch_tile, api_key=carto_api_key),
            credential_cache_key=sha256(carto_api_key.encode()).hexdigest(),
        )
    return definition
