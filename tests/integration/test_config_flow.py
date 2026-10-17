from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import LocationSelector, SelectSelector
from pytest_homeassistant_custom_component.common import MockConfigEntry
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE

from custom_components.rain_incoming.const import (
    CONF_FORECAST_CONFIDENCE_ENABLED,
    CONF_LOCATION_NAME,
    CONF_MAP_STYLE,
    CONF_SATELLITE_CONFIDENCE_ENABLED,
    DOMAIN,
)
from .conftest import setup_integration
from custom_components.rain_incoming.radar.detector import Confidence, DetectionResult

_MOCK_RESULT = DetectionResult(
    rain_incoming=False,
    arrival_time=None,
    confidence=Confidence.UNAVAILABLE,
    frame_count=0,
    max_approaching_intensity=0.0,
)

_SYDNEY = {"latitude": -33.701, "longitude": 151.209}


def _loc(lat: float = -33.701, lon: float = 151.209) -> dict:
    """Build a location dict for LocationSelector input."""
    return {"latitude": lat, "longitude": lon}


@pytest.fixture(autouse=True)
def mock_coverage_check():
    """Mock the coverage check to return True (covered) by default."""
    with patch(
        "custom_components.rain_incoming.config_flow.RainIncomingConfigFlow._check_coverage",
        new=AsyncMock(return_value=True),
    ):
        yield


@pytest.mark.asyncio
async def test_config_flow_shows_form(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_config_flow_creates_entry_with_valid_input(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["latitude"] == -33.701
    assert result["data"]["longitude"] == 151.209
    assert result["data"]["lookahead_minutes"] == 60


@pytest.mark.asyncio
async def test_config_flow_uses_location_selector(hass: HomeAssistant):
    """Config flow schema must use LocationSelector for the location field."""
    from custom_components.rain_incoming.config_flow import _build_schema

    schema = _build_schema(default_lat=-33.701, default_lon=151.209)
    for key in schema.schema:
        if getattr(key, "schema", None) == "location":
            assert isinstance(schema.schema[key], LocationSelector), (
                f"location field should use LocationSelector, "
                f"got {type(schema.schema[key]).__name__}"
            )
            return
    pytest.fail("'location' field not found in config flow schema")


@pytest.mark.asyncio
async def test_config_flow_title_uses_location_name_when_provided(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
            "location_name": "Beach House",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Rain Incoming - Beach House"
    assert result["data"]["location_name"] == "Beach House"


@pytest.mark.asyncio
async def test_config_flow_title_uses_coordinates_when_name_empty(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
            "location_name": "",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Rain Incoming (-33.70, 151.21)"


@pytest.mark.asyncio
async def test_config_flow_title_uses_coordinates_when_name_omitted(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Rain Incoming (-33.70, 151.21)"


@pytest.mark.asyncio
async def test_config_flow_rejects_lookahead_out_of_range(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 200,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert "lookahead_minutes" in result["errors"]


# ---------------------------------------------------------------------------
# PR 3: map_style field + location_name validator + options flow + migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_flow_rejects_location_name_over_16_chars(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
            "location_name": "A" * 17,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert "location_name" in result["errors"]


@pytest.mark.asyncio
async def test_config_flow_stores_map_style_in_entry(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
            "map_style": "osm_dark",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAP_STYLE] == "osm_dark"


@pytest.mark.asyncio
async def test_config_flow_defaults_map_style_to_voyager(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"].get(CONF_MAP_STYLE) == "voyager"


@pytest.mark.asyncio
async def test_config_flow_rejects_invalid_map_style(hass: HomeAssistant):
    from homeassistant.data_entry_flow import InvalidData

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "location": _loc(),
                "lookahead_minutes": 60,
                "map_style": "not_a_real_style",
            },
        )


@pytest.mark.asyncio
async def test_options_flow_changes_map_style(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
        },
        version=2,
    )
    await setup_integration(hass, entry, _MOCK_RESULT)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "location_name": "Beach House",
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "esri_imagery",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # Options flow stores all values in entry.data (not entry.options) to
    # avoid dual storage. The coordinator reads from data only.
    assert entry.data[CONF_MAP_STYLE] == "esri_imagery"


@pytest.mark.asyncio
async def test_options_flow_rejects_location_name_over_16_chars(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
        },
        version=2,
    )
    await setup_integration(hass, entry, _MOCK_RESULT)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "location_name": "B" * 17,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert "location_name" in result["errors"]


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_adds_map_style_to_v1_entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
        },
        version=1,
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert entry.data.get(CONF_MAP_STYLE) == "voyager"


@pytest.mark.asyncio
async def test_migration_preserves_long_location_name(hass: HomeAssistant):
    long_name = "A" * 50
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            "location_name": long_name,
        },
        version=1,
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert entry.data.get(CONF_MAP_STYLE) == "voyager"
    assert entry.data["location_name"] == long_name


@pytest.mark.asyncio
async def test_migration_does_not_re_migrate_v2_entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "osm_dark",
        },
        version=2,
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert entry.data[CONF_MAP_STYLE] == "osm_dark"


# ---------------------------------------------------------------------------
# Coordinator wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_reads_map_style_from_data(hass: HomeAssistant):
    """map_style reads from entry.data (canonical source). Options are ignored."""
    from custom_components.rain_incoming.coordinator import RainDetectorCoordinator

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "esri_imagery",
        },
        options={},
        version=2,
    )
    entry.add_to_hass(hass)

    coordinator = RainDetectorCoordinator(hass, entry)
    assert coordinator.map_style == "esri_imagery"


@pytest.mark.asyncio
async def test_coordinator_falls_back_to_data_map_style(hass: HomeAssistant):
    from custom_components.rain_incoming.coordinator import RainDetectorCoordinator

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "osm_standard",
        },
        options={},
        version=2,
    )
    entry.add_to_hass(hass)

    coordinator = RainDetectorCoordinator(hass, entry)
    assert coordinator.map_style == "osm_standard"


@pytest.mark.asyncio
async def test_coordinator_defaults_to_voyager_when_no_style_set(hass: HomeAssistant):
    from custom_components.rain_incoming.coordinator import RainDetectorCoordinator

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
        },
        options={},
        version=2,
    )
    entry.add_to_hass(hass)

    coordinator = RainDetectorCoordinator(hass, entry)
    assert coordinator.map_style == "voyager"


# ---------------------------------------------------------------------------
# Selector type tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_style_field_uses_select_selector_in_config_flow(hass: HomeAssistant):
    from custom_components.rain_incoming.config_flow import _build_schema

    schema = _build_schema(default_lat=-33.701, default_lon=151.209)
    for key in schema.schema:
        if getattr(key, "schema", None) == CONF_MAP_STYLE:
            assert isinstance(schema.schema[key], SelectSelector)
            return
    pytest.fail("CONF_MAP_STYLE not found in config flow schema")


@pytest.mark.asyncio
async def test_map_style_field_uses_select_selector_in_options_flow(hass: HomeAssistant):
    from custom_components.rain_incoming.config_flow import _build_options_schema

    schema = _build_options_schema()
    for key in schema.schema:
        if getattr(key, "schema", None) == CONF_MAP_STYLE:
            assert isinstance(schema.schema[key], SelectSelector)
            return
    pytest.fail("CONF_MAP_STYLE not found in options flow schema")


# ---------------------------------------------------------------------------
# Options flow reload + boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_flow_schedules_reload_after_save(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
        },
        version=2,
    )
    await setup_integration(hass, entry, _MOCK_RESULT)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as mock_reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "location_name": "Beach House",
                "lookahead_minutes": 60,
                CONF_MAP_STYLE: "esri_imagery",
            },
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_config_flow_accepts_location_name_exactly_16_chars(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
            "location_name": "A" * 16,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOCATION_NAME] == "A" * 16


# ---------------------------------------------------------------------------
# Location limit (MAX_LOCATIONS = 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_flow_rejects_when_at_location_limit(hass: HomeAssistant):
    for i in range(4):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "latitude": float(i),
                "longitude": float(i),
                "lookahead_minutes": 60,
                CONF_MAP_STYLE: "voyager",
            },
            version=2,
        )
        entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "too_many_locations"


@pytest.mark.asyncio
async def test_config_flow_accepts_when_below_location_limit(hass: HomeAssistant):
    for i in range(3):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "latitude": float(i),
                "longitude": float(i),
                "lookahead_minutes": 60,
                CONF_MAP_STYLE: "voyager",
            },
            version=2,
        )
        entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_config_flow_allows_when_disabled_entries_free_slots(hass: HomeAssistant):
    """4 entries with 1 disabled = 3 enabled. Should allow a 4th enabled entry."""
    from homeassistant.config_entries import ConfigEntryDisabler

    for i in range(4):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "latitude": float(i),
                "longitude": float(i),
                "lookahead_minutes": 60,
                CONF_MAP_STYLE: "voyager",
            },
            version=2,
            disabled_by=ConfigEntryDisabler.USER if i == 0 else None,
        )
        entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM, (
        f"With 1 disabled entry, should allow adding (3 enabled < 4 max). "
        f"Got: {result['type']}"
    )


@pytest.mark.asyncio
async def test_config_flow_rejects_when_above_location_limit(hass: HomeAssistant):
    for i in range(5):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "latitude": float(i),
                "longitude": float(i),
                "lookahead_minutes": 60,
                CONF_MAP_STYLE: "voyager",
            },
            version=2,
        )
        entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "too_many_locations"


# ---------------------------------------------------------------------------
# Sticky inputs on validation error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_flow_preserves_user_input_on_validation_error(hass: HomeAssistant):
    """When validation fails the form re-renders with the user's typed values."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 45,
            "location_name": "X" * 40,
            "map_style": "voyager",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert "location_name" in result["errors"]

    schema = result["data_schema"]
    defaults = {
        (key.schema if hasattr(key, "schema") else str(key)): (
            key.default() if callable(getattr(key, "default", None)) else None
        )
        for key in schema.schema.keys()
    }

    assert defaults.get("lookahead_minutes") == 45, (
        f"Expected lookahead_minutes default to be 45, got {defaults.get('lookahead_minutes')}"
    )
    location_default = defaults.get("location")
    assert location_default == {"latitude": -33.701, "longitude": 151.209}, (
        f"Expected location default to preserve user's coordinates, got {location_default}"
    )


@pytest.mark.asyncio
async def test_options_flow_preserves_user_input_on_validation_error(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
        },
        version=2,
    )
    await setup_integration(hass, entry, _MOCK_RESULT)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "location_name": "Y" * 40,
            "lookahead_minutes": 25,
            CONF_MAP_STYLE: "osm_dark",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert "location_name" in result["errors"]

    schema = result["data_schema"]
    defaults = {
        (key.schema if hasattr(key, "schema") else str(key)): (
            key.default() if callable(getattr(key, "default", None)) else None
        )
        for key in schema.schema.keys()
    }

    assert defaults.get("lookahead_minutes") == 25, (
        f"Expected lookahead_minutes default to be 25, got {defaults.get('lookahead_minutes')}"
    )


# ---------------------------------------------------------------------------
# Translation file checks
# ---------------------------------------------------------------------------


def test_translation_includes_location_name_max_length():
    import json
    import os

    path = os.path.join(
        os.path.dirname(__file__),
        "../../custom_components/rain_incoming/translations/en.json",
    )
    with open(path) as f:
        translations = json.load(f)

    label = translations["config"]["step"]["user"]["data"]["location_name"]
    assert "30" in label
    assert "character" in label.lower()


def test_translation_includes_lookahead_range_in_config_flow():
    import json
    import os

    path = os.path.join(
        os.path.dirname(__file__),
        "../../custom_components/rain_incoming/translations/en.json",
    )
    with open(path) as f:
        translations = json.load(f)

    label = translations["config"]["step"]["user"]["data"]["lookahead_minutes"]
    assert "20" in label and "60" in label


def test_translation_includes_location_name_max_length_in_options_flow():
    import json
    import os

    path = os.path.join(
        os.path.dirname(__file__),
        "../../custom_components/rain_incoming/translations/en.json",
    )
    with open(path) as f:
        translations = json.load(f)

    label = translations["options"]["step"]["init"]["data"]["location_name"]
    assert "30" in label
    assert "character" in label.lower()


def test_translation_includes_lookahead_range_in_options_flow():
    import json
    import os

    path = os.path.join(
        os.path.dirname(__file__),
        "../../custom_components/rain_incoming/translations/en.json",
    )
    with open(path) as f:
        translations = json.load(f)

    label = translations["options"]["step"]["init"]["data"]["lookahead_minutes"]
    assert "20" in label and "60" in label


# ---------------------------------------------------------------------------
# Lat/lon immutability via options flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_flow_does_not_modify_latitude_longitude(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
        },
        version=2,
    )
    await setup_integration(hass, entry, _MOCK_RESULT)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MAP_STYLE: "esri_imagery",
            "location_name": "Updated Name",
            "lookahead_minutes": 60,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    assert entry.data[CONF_LATITUDE] == -33.701
    assert entry.data[CONF_LONGITUDE] == 151.209


# ---------------------------------------------------------------------------
# Coverage check tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_flow_creates_entry_when_coverage_confirmed(
    hass: HomeAssistant,
):
    """When coverage check returns True, entry is created directly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(),
            "lookahead_minutes": 60,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_config_flow_shows_confirm_when_no_coverage(
    hass: HomeAssistant, mock_coverage_check,
):
    """When coverage check returns False, show confirmation step."""
    with patch(
        "custom_components.rain_incoming.config_flow.RainIncomingConfigFlow._check_coverage",
        new=AsyncMock(return_value=False),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "location": _loc(0.0, 0.0),
                "lookahead_minutes": 60,
            },
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm_no_coverage"


@pytest.mark.asyncio
async def test_config_flow_creates_entry_after_confirm_no_coverage(
    hass: HomeAssistant, mock_coverage_check,
):
    """User confirming the no-coverage warning creates the entry."""
    with patch(
        "custom_components.rain_incoming.config_flow.RainIncomingConfigFlow._check_coverage",
        new=AsyncMock(return_value=False),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "location": _loc(0.0, 0.0),
                "lookahead_minutes": 60,
                "location_name": "Middle of Ocean",
            },
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm_no_coverage"

    # User confirms
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["latitude"] == 0.0
    assert result["data"]["longitude"] == 0.0
    assert result["data"]["location_name"] == "Middle of Ocean"


@pytest.mark.asyncio
async def test_config_flow_creates_entry_when_coverage_check_fails(
    hass: HomeAssistant, mock_coverage_check,
):
    """When coverage check raises an exception, fail open and create entry."""
    with patch(
        "custom_components.rain_incoming.config_flow.RainIncomingConfigFlow._check_coverage",
        new=AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "location": _loc(),
                "lookahead_minutes": 60,
            },
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_config_flow_stores_flat_lat_lon_not_location_dict(hass: HomeAssistant):
    """Entry data must contain flat latitude/longitude keys, not the location dict."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "location": _loc(-37.814, 144.963),
            "lookahead_minutes": 60,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert "location" not in result["data"]
    assert result["data"]["latitude"] == -37.814
    assert result["data"]["longitude"] == 144.963


@pytest.mark.asyncio
async def test_options_flow_persists_forecast_confidence_enabled(hass: HomeAssistant):
    """W3: submitting enable_forecast_confidence=True via OptionsFlow persists it to entry.data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
        },
        version=2,
    )
    await setup_integration(hass, entry, _MOCK_RESULT)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
            CONF_FORECAST_CONFIDENCE_ENABLED: True,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_FORECAST_CONFIDENCE_ENABLED] is True, (
        f"Expected CONF_FORECAST_CONFIDENCE_ENABLED=True in entry.data, got {entry.data.get(CONF_FORECAST_CONFIDENCE_ENABLED)!r}"
    )


@pytest.mark.asyncio
async def test_options_flow_satellite_confidence_toggle(hass: HomeAssistant):
    """Submitting CONF_SATELLITE_CONFIDENCE_ENABLED=True via OptionsFlow persists it to entry.data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
        },
        version=2,
    )
    await setup_integration(hass, entry, _MOCK_RESULT)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
            CONF_MAP_STYLE: "voyager",
            CONF_SATELLITE_CONFIDENCE_ENABLED: True,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_SATELLITE_CONFIDENCE_ENABLED] is True, (
        f"Expected CONF_SATELLITE_CONFIDENCE_ENABLED=True in entry.data, got {entry.data.get(CONF_SATELLITE_CONFIDENCE_ENABLED)!r}"
    )


@pytest.mark.parametrize("style", ["voyager", "dark_matter", "osm_standard"])
async def test_setup_saves_carto_key(hass, style):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "location": _loc(), "lookahead_minutes": 60,
        "map_style": style, "carto_api_key": "  setup-key  ",
    })
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["carto_api_key"] == "setup-key"


@pytest.mark.parametrize("new_key", ["replacement-key", ""])
async def test_options_update_or_clear_carto_key(hass, new_key):
    entry = MockConfigEntry(domain=DOMAIN, data={
        **_SYDNEY, "lookahead_minutes": 60, "map_style": "voyager",
        "carto_api_key": "old-key",
    }, version=2)
    await setup_integration(hass, entry, _MOCK_RESULT)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "lookahead_minutes": 60, "map_style": "dark_matter",
        "carto_api_key": new_key,
    })
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data["carto_api_key"] == new_key


def test_carto_key_form_masks_password_and_preserves_defaults():
    from custom_components.rain_incoming.config_flow import (
        _build_schema, _resolve_options_schema_defaults,
    )
    from homeassistant.helpers.selector import TextSelector

    schemas = [
        _build_schema(0, 0),
        _resolve_options_schema_defaults({"carto_api_key": "saved-key"}, None),
        _resolve_options_schema_defaults({"carto_api_key": "saved-key"}, {
            "carto_api_key": "typed-key", "lookahead_minutes": 0,
        }),
    ]
    for schema, expected in zip(schemas, ["", "saved-key", "typed-key"]):
        key = next(k for k in schema.schema if k.schema == "carto_api_key")
        assert key.default() == expected
        selector = schema.schema[key]
        assert isinstance(selector, TextSelector)
        assert selector.config["type"] == "password"
