"""Unit tests for map_styles.py - selectable base map style module.

Tests cover:
- Registry lookup by enum and string
- Validation / error paths
- Per-style tile fetch logic (all mocked - no real network calls)
- OSM Dark transform correctness
- ESRI multi-layer compositing and URL correctness
"""
from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from custom_components.rain_incoming.radar.map_styles import (
    MapStyle,
    MapStyleDefinition,
    _OSM_DARK_SATURATION_FACTOR,
    _apply_osm_dark_transform,
    get_style,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_png_bytes(size=(256, 256), color=(100, 150, 200, 255)) -> bytes:
    """Create a tiny valid RGBA PNG."""
    img = Image.new("RGBA", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _MockResponse:
    """Mock aiohttp response compatible with fetch_with_retry's direct-await pattern.

    fetch_with_retry does `resp = await session.get(url, timeout=...)` and then
    reads resp.status, resp.headers, resp.raise_for_status(), resp.read(), resp.release()
    directly - NOT as a context manager.
    """

    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body
        self.headers: dict = {}

    async def read(self) -> bytes:
        return self._body

    def raise_for_status(self) -> None:
        if self.status >= 400:
            import aiohttp
            raise aiohttp.ClientResponseError(
                request_info=None,  # type: ignore[arg-type]
                history=(),
                status=self.status,
            )

    def release(self) -> None:
        pass


class _TrackingSession:
    """Minimal mock aiohttp session that records URLs fetched.

    session.get() must be an async function returning a response directly,
    not a context manager - to match fetch_with_retry's calling convention.
    """

    def __init__(self, url_to_bytes: dict[str, bytes]) -> None:
        self._url_to_bytes = url_to_bytes
        self.calls: list[str] = []

    async def get(self, url: str, **kwargs):
        self.calls.append(url)
        if url in self._url_to_bytes:
            return _MockResponse(status=200, body=self._url_to_bytes[url])
        return _MockResponse(status=404)


def _mock_session(url_to_bytes: dict[str, bytes]) -> _TrackingSession:
    """Build a mock aiohttp session that returns specific bytes per URL."""
    return _TrackingSession(url_to_bytes)


def _calls_made(session: _TrackingSession) -> list[str]:
    """Return the list of URLs passed to session.get()."""
    return session.calls


# ---------------------------------------------------------------------------
# Registry lookup tests
# ---------------------------------------------------------------------------

def test_get_style_by_enum_returns_definition():
    defn = get_style(MapStyle.OSM_STANDARD)
    assert isinstance(defn, MapStyleDefinition)
    assert defn.attribution


def test_get_style_by_string_returns_definition():
    defn = get_style("osm_dark")
    assert isinstance(defn, MapStyleDefinition)
    assert defn.style == MapStyle.OSM_DARK


def test_get_style_unknown_string_raises():
    with pytest.raises(ValueError, match="nonexistent"):
        get_style("nonexistent")


def test_all_styles_registered():
    for style in MapStyle:
        defn = get_style(style)
        assert defn.style == style


def test_all_attributions_nonempty():
    expected_substrings = {
        MapStyle.VOYAGER: "CARTO",
        MapStyle.OSM_STANDARD: "OpenStreetMap",
        MapStyle.OSM_DARK: "OpenStreetMap",
        MapStyle.ESRI_IMAGERY: "Esri",
        MapStyle.DARK_MATTER: "CARTO",
    }
    for style in MapStyle:
        defn = get_style(style)
        assert defn.attribution, f"{style} has empty attribution"
        assert expected_substrings[style] in defn.attribution, (
            f"{style} attribution missing expected provider. Got: {defn.attribution!r}"
        )


# ---------------------------------------------------------------------------
# Dark Matter tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dark_matter_fetches_from_correct_url():
    expected_url = "https://basemaps.cartocdn.com/dark_all/7/115/78.png"
    session = _mock_session({expected_url: _make_png_bytes()})

    defn = get_style(MapStyle.DARK_MATTER)
    await defn.fetch_tile(session, 7, 115, 78)

    assert expected_url in _calls_made(session)


@pytest.mark.asyncio
async def test_dark_matter_returns_rgba_image():
    url = "https://basemaps.cartocdn.com/dark_all/7/115/78.png"
    session = _mock_session({url: _make_png_bytes()})

    defn = get_style(MapStyle.DARK_MATTER)
    result = await defn.fetch_tile(session, 7, 115, 78)

    assert result is not None
    assert isinstance(result, Image.Image)
    assert result.mode == "RGBA"
    assert result.size == (256, 256)


@pytest.mark.asyncio
async def test_dark_matter_returns_none_on_404():
    session = _mock_session({})  # all URLs 404

    defn = get_style(MapStyle.DARK_MATTER)
    result = await defn.fetch_tile(session, 7, 115, 78)

    assert result is None


# ---------------------------------------------------------------------------
# Voyager tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voyager_fetches_from_correct_url():
    expected_url = "https://basemaps.cartocdn.com/rastertiles/voyager/7/115/78.png"
    session = _mock_session({expected_url: _make_png_bytes()})

    defn = get_style(MapStyle.VOYAGER)
    await defn.fetch_tile(session, 7, 115, 78)

    assert expected_url in _calls_made(session)


@pytest.mark.asyncio
async def test_voyager_returns_rgba_image():
    url = "https://basemaps.cartocdn.com/rastertiles/voyager/7/115/78.png"
    session = _mock_session({url: _make_png_bytes()})

    defn = get_style(MapStyle.VOYAGER)
    result = await defn.fetch_tile(session, 7, 115, 78)

    assert result is not None
    assert isinstance(result, Image.Image)
    assert result.mode == "RGBA"
    assert result.size == (256, 256)


@pytest.mark.asyncio
async def test_voyager_returns_none_on_404():
    session = _mock_session({})  # all URLs 404

    defn = get_style(MapStyle.VOYAGER)
    result = await defn.fetch_tile(session, 7, 115, 78)

    assert result is None


# ---------------------------------------------------------------------------
# OSM Standard tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_osm_standard_fetches_from_correct_url():
    expected_url = "https://tile.openstreetmap.org/7/115/78.png"
    session = _mock_session({expected_url: _make_png_bytes()})

    defn = get_style(MapStyle.OSM_STANDARD)
    await defn.fetch_tile(session, 7, 115, 78)

    assert expected_url in _calls_made(session)


@pytest.mark.asyncio
async def test_osm_standard_returns_rgba_image():
    url = "https://tile.openstreetmap.org/7/115/78.png"
    session = _mock_session({url: _make_png_bytes()})

    defn = get_style(MapStyle.OSM_STANDARD)
    result = await defn.fetch_tile(session, 7, 115, 78)

    assert result is not None
    assert isinstance(result, Image.Image)
    assert result.mode == "RGBA"
    assert result.size == (256, 256)


@pytest.mark.asyncio
async def test_osm_standard_returns_none_on_404():
    session = _mock_session({})  # all URLs 404

    defn = get_style(MapStyle.OSM_STANDARD)
    result = await defn.fetch_tile(session, 7, 115, 78)

    assert result is None


# ---------------------------------------------------------------------------
# OSM Dark tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_osm_dark_fetches_osm_url():
    osm_url = "https://tile.openstreetmap.org/7/115/78.png"
    session = _mock_session({osm_url: _make_png_bytes()})

    defn = get_style(MapStyle.OSM_DARK)
    await defn.fetch_tile(session, 7, 115, 78)

    assert osm_url in _calls_made(session)


@pytest.mark.asyncio
async def test_osm_dark_applies_hsv_transform():
    """A mid-tone pale pixel (like OSM land) should become darker after the transform."""
    # pale beige - like OSM land area. In HSV this has high V.
    osm_url = "https://tile.openstreetmap.org/7/115/78.png"
    session = _mock_session({osm_url: _make_png_bytes(color=(200, 180, 150, 255))})

    defn = get_style(MapStyle.OSM_DARK)
    result = await defn.fetch_tile(session, 7, 115, 78)

    assert result is not None
    result_arr = np.array(result.convert("HSV"))
    # The pale beige original has high V (bright). After invert, V should be low (dark).
    mean_v = result_arr[:, :, 2].mean()
    original_v = np.array(Image.new("RGB", (256, 256), (200, 180, 150)).convert("HSV"))[:, :, 2].mean()
    assert mean_v < original_v, (
        f"OSM dark transform should reduce brightness. "
        f"Original mean V={original_v:.1f}, result mean V={mean_v:.1f}"
    )


@pytest.mark.asyncio
async def test_osm_dark_brightens_dark_features():
    """An all-black tile with dark features should become bright after feature lift."""
    osm_url = "https://tile.openstreetmap.org/7/115/78.png"
    # All-black PNG: V=0, so after V-invert V=255, and feature_lift for black luma=0 -> 255
    session = _mock_session({osm_url: _make_png_bytes(color=(0, 0, 0, 255))})

    defn = get_style(MapStyle.OSM_DARK)
    result = await defn.fetch_tile(session, 7, 115, 78)

    assert result is not None
    result_arr = np.array(result.convert("RGB"))
    # Black inverted = white (V=0 -> V=255, feature_lift 255-0=255)
    mean_val = result_arr.mean()
    assert mean_val > 200, (
        f"All-black tile should become very bright after feature lift. Got mean={mean_val:.1f}"
    )


@pytest.mark.asyncio
async def test_osm_dark_returns_none_if_base_fails():
    session = _mock_session({})  # all 404

    defn = get_style(MapStyle.OSM_DARK)
    result = await defn.fetch_tile(session, 7, 115, 78)

    assert result is None


def test_osm_dark_saturation_factor_affects_output():
    """The saturation factor constant must exist and be < 1.0 (i.e., desaturating)."""
    assert _OSM_DARK_SATURATION_FACTOR < 1.0, (
        f"_OSM_DARK_SATURATION_FACTOR should be < 1.0 to reduce saturation, "
        f"got {_OSM_DARK_SATURATION_FACTOR}"
    )


# ---------------------------------------------------------------------------
# ESRI Imagery tests
# ---------------------------------------------------------------------------

def _esri_url(layer: str, z: int, x: int, y: int) -> str:
    return (
        f"https://server.arcgisonline.com/ArcGIS/rest/services"
        f"/{layer}/MapServer/tile/{z}/{y}/{x}"
    )


_ESRI_BASE = "World_Imagery"
_ESRI_ROADS = "Reference/World_Transportation"
_ESRI_LABELS = "Reference/World_Boundaries_and_Places"


@pytest.mark.asyncio
async def test_esri_fetches_three_urls():
    z, x, y = 7, 115, 78
    urls = {
        _esri_url(_ESRI_BASE, z, x, y): _make_png_bytes(color=(40, 60, 40, 255)),
        _esri_url(_ESRI_ROADS, z, x, y): _make_png_bytes(color=(200, 200, 200, 128)),
        _esri_url(_ESRI_LABELS, z, x, y): _make_png_bytes(color=(255, 255, 255, 200)),
    }
    session = _mock_session(urls)

    defn = get_style(MapStyle.ESRI_IMAGERY)
    await defn.fetch_tile(session, z, x, y)

    called = _calls_made(session)
    for url in urls:
        assert url in called, f"Expected {url!r} to be fetched, got {called}"


@pytest.mark.asyncio
async def test_esri_composites_three_layers():
    """Three distinct-coloured layers composited should show the top layer's colour dominating."""
    z, x, y = 7, 115, 78
    # Base: dark green (satellite-ish), Roads: grey semi-transparent, Labels: white mostly-opaque
    base_png = _make_png_bytes(color=(40, 80, 40, 255))
    roads_png = _make_png_bytes(color=(180, 180, 180, 100))
    labels_png = _make_png_bytes(color=(240, 240, 240, 220))
    urls = {
        _esri_url(_ESRI_BASE, z, x, y): base_png,
        _esri_url(_ESRI_ROADS, z, x, y): roads_png,
        _esri_url(_ESRI_LABELS, z, x, y): labels_png,
    }
    session = _mock_session(urls)

    defn = get_style(MapStyle.ESRI_IMAGERY)
    result = await defn.fetch_tile(session, z, x, y)

    assert result is not None
    arr = np.array(result.convert("RGB"))
    mean_r = arr[:, :, 0].mean()
    mean_g = arr[:, :, 1].mean()

    # Labels layer (high R,G,B) composited last should raise the R channel significantly
    # above the dark base green (~40 R). With alpha 220/255 the blend pulls heavily toward 240.
    assert mean_r > 150, (
        f"Composited result should reflect high-alpha labels overlay. Mean R={mean_r:.1f}"
    )
    # Green channel raised too by labels (240), but base was already (80 G)
    assert mean_g > 150, (
        f"Green channel should be elevated from labels overlay. Mean G={mean_g:.1f}"
    )


@pytest.mark.asyncio
async def test_esri_succeeds_without_overlays():
    """If road/label overlays 404, should still return the base image."""
    z, x, y = 7, 115, 78
    base_color = (40, 80, 40, 255)
    urls = {
        _esri_url(_ESRI_BASE, z, x, y): _make_png_bytes(color=base_color),
        # roads and labels intentionally missing -> 404
    }
    session = _mock_session(urls)

    defn = get_style(MapStyle.ESRI_IMAGERY)
    result = await defn.fetch_tile(session, z, x, y)

    assert result is not None, "Should return base image when overlays fail"
    arr = np.array(result.convert("RGB"))
    # Result should still look like the base (dark green ~40 R, ~80 G)
    assert arr[:, :, 0].mean() < 80, "Without overlays, R channel should still be ~base"


@pytest.mark.asyncio
async def test_esri_returns_none_if_base_fails():
    """If base 404, should return None even if overlays would succeed."""
    z, x, y = 7, 115, 78
    urls = {
        # base missing, overlays present
        _esri_url(_ESRI_ROADS, z, x, y): _make_png_bytes(color=(200, 200, 200, 128)),
        _esri_url(_ESRI_LABELS, z, x, y): _make_png_bytes(color=(255, 255, 255, 200)),
    }
    session = _mock_session(urls)

    defn = get_style(MapStyle.ESRI_IMAGERY)
    result = await defn.fetch_tile(session, z, x, y)

    assert result is None, "Should return None when base tile fails"


@pytest.mark.asyncio
async def test_esri_uses_y_x_url_order():
    """Regression: ESRI URLs must use /{z}/{y}/{x} not /{z}/{x}/{y}."""
    z, x, y = 7, 115, 78
    # Correct URL has y=78 before x=115
    correct_base_url = _esri_url(_ESRI_BASE, z, x, y)
    assert f"/{z}/{y}/{x}" in correct_base_url, "Helper sanity check"

    # Feed the mock only the correct URL so a wrong-order URL would get a 404
    session = _mock_session({
        correct_base_url: _make_png_bytes(),
        _esri_url(_ESRI_ROADS, z, x, y): _make_png_bytes(),
        _esri_url(_ESRI_LABELS, z, x, y): _make_png_bytes(),
    })

    defn = get_style(MapStyle.ESRI_IMAGERY)
    result = await defn.fetch_tile(session, z, x, y)

    assert result is not None, (
        "ESRI fetch should succeed with correct y/x URL order. "
        "If this fails, the implementation may be using x/y order."
    )

    called = _calls_made(session)
    for url in called:
        if "arcgisonline" in url:
            # Parse the last three path segments - should be z/y/x
            parts = url.rstrip("/").split("/")
            tile_z, tile_y, tile_x = parts[-3], parts[-2], parts[-1]
            assert tile_y == str(y), (
                f"ESRI URL should have y={y} before x={x}, got .../{tile_z}/{tile_y}/{tile_x} in {url}"
            )
            assert tile_x == str(x), (
                f"ESRI URL should have x={x} last, got .../{tile_z}/{tile_y}/{tile_x} in {url}"
            )


# ---------------------------------------------------------------------------
# is_dark flag correctness (issue #70)
# ---------------------------------------------------------------------------

def test_esri_imagery_is_dark():
    """ESRI satellite imagery has a dark/complex background - must use white text."""
    defn = get_style(MapStyle.ESRI_IMAGERY)
    assert defn.is_dark is True, (
        "ESRI World Imagery must be marked is_dark=True so labels render with "
        "white text. Satellite backgrounds make black text illegible."
    )


def test_dark_matter_is_dark():
    """Dark Matter is a dark basemap - must use white text."""
    assert get_style(MapStyle.DARK_MATTER).is_dark is True


def test_osm_dark_is_dark():
    """OSM Dark is a dark basemap - must use white text."""
    assert get_style(MapStyle.OSM_DARK).is_dark is True


def test_light_styles_are_not_dark():
    """Voyager and OSM Standard are light maps - must use dark text."""
    assert get_style(MapStyle.VOYAGER).is_dark is False
    assert get_style(MapStyle.OSM_STANDARD).is_dark is False


@pytest.mark.parametrize("style", [MapStyle.VOYAGER, MapStyle.DARK_MATTER])
async def test_carto_key_is_sent_as_query_parameter(style):
    from unittest.mock import AsyncMock, patch

    with patch(
        "custom_components.rain_incoming.radar.map_styles.fetch_with_retry",
        new_callable=AsyncMock,
    ) as fetch:
        fetch.return_value.read = AsyncMock(return_value=_make_png_bytes())
        result = await get_style(style, carto_api_key="secret & key").fetch_tile(
            MagicMock(), 7, 115, 78
        )
    assert result is not None
    assert fetch.call_args.kwargs["params"] == {"key": "secret & key"}
    assert "secret" not in fetch.call_args.args[1]


async def test_carto_key_does_not_reach_other_providers():
    url = "https://tile.openstreetmap.org/7/115/78.png"
    session = _mock_session({url: _make_png_bytes()})
    result = await get_style("osm_standard", carto_api_key="secret").fetch_tile(
        session, 7, 115, 78
    )
    assert result is not None
    assert session.calls == [url]


async def test_carto_key_changes_do_not_reuse_watermarked_cached_tiles():
    from custom_components.rain_incoming.radar.composite import (
        _fetch_map_tile, clear_tile_caches,
    )
    from unittest.mock import AsyncMock, patch

    clear_tile_caches()
    try:
        with patch(
            "custom_components.rain_incoming.radar.map_styles.fetch_with_retry",
            new_callable=AsyncMock,
        ) as fetch:
            fetch.return_value.read = AsyncMock(return_value=_make_png_bytes())
            for key in ("", "first", "second", "second"):
                await _fetch_map_tile(
                    MagicMock(), 7, 115, 78,
                    get_style("voyager", carto_api_key=key),
                )
        assert fetch.await_count == 3
    finally:
        clear_tile_caches()


async def test_carto_failure_does_not_log_key(caplog):
    from unittest.mock import AsyncMock, patch

    with patch(
        "custom_components.rain_incoming.radar.map_styles.fetch_with_retry",
        new_callable=AsyncMock,
        side_effect=RuntimeError("request failed: https://example.com/?key=secret-key"),
    ):
        result = await get_style("voyager", carto_api_key="secret-key").fetch_tile(
            MagicMock(), 7, 115, 78
        )
    assert result is None
    assert "secret-key" not in caplog.text
    assert "Map tile fetch error" in caplog.text


@pytest.mark.parametrize("style,path", [
    ("voyager", "rastertiles/voyager"), ("dark_matter", "dark_all"),
])
async def test_carto_request_encodes_key_and_decodes_tile(style, path):
    import aiohttp
    from aioresponses import aioresponses
    from yarl import URL

    url = URL(f"https://basemaps.cartocdn.com/{path}/7/115/78.png").with_query(
        key="secret & key"
    )
    with aioresponses() as responses:
        responses.get(url, body=_make_png_bytes())
        async with aiohttp.ClientSession() as session:
            result = await get_style(style, carto_api_key="secret & key").fetch_tile(
                session, 7, 115, 78
            )
    assert result is not None
    assert result.size == (256, 256)
