from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rain_incoming.const import DOMAIN
from custom_components.rain_incoming.diagnostics import async_get_config_entry_diagnostics
from custom_components.rain_incoming.radar.detector import Confidence, DetectionResult
from tests.integration.conftest import setup_integration

MOCK_RESULT_RAIN = DetectionResult(
    rain_incoming=True,
    arrival_time=datetime(2026, 4, 7, 10, 30, tzinfo=timezone.utc),
    confidence=Confidence.NORMAL,
    frame_count=6,
    max_approaching_intensity=0.65,
)


@pytest.fixture
def mock_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "latitude": -33.701,
            "longitude": 151.209,
            "lookahead_minutes": 60,
        },
        entry_id="test_diag_123",
    )


@pytest.mark.asyncio
async def test_diagnostics_returns_expected_structure(hass: HomeAssistant, mock_entry):
    await setup_integration(hass, mock_entry, MOCK_RESULT_RAIN)

    diag = await async_get_config_entry_diagnostics(hass, mock_entry)

    assert "config" in diag
    assert "coordinator" in diag
    assert "detection" in diag


@pytest.mark.asyncio
async def test_diagnostics_anonymizes_location(hass: HomeAssistant, mock_entry):
    await setup_integration(hass, mock_entry, MOCK_RESULT_RAIN)

    diag = await async_get_config_entry_diagnostics(hass, mock_entry)

    # Original: -33.701 -> rounded to 1 decimal: -33.7
    assert diag["config"]["latitude"] == -33.7
    # Original: 151.209 -> rounded to 1 decimal: 151.2
    assert diag["config"]["longitude"] == 151.2


@pytest.mark.asyncio
async def test_diagnostics_includes_coordinator_state(hass: HomeAssistant, mock_entry):
    await setup_integration(hass, mock_entry, MOCK_RESULT_RAIN)

    diag = await async_get_config_entry_diagnostics(hass, mock_entry)

    coord = diag["coordinator"]
    assert "last_update_success" in coord
    assert "last_update_success_time" in coord
    assert "latest_frame_path" in coord
    assert "last_rain_nearby_time" in coord
    assert "consecutive_failures" in coord
    assert "update_interval_seconds" in coord


@pytest.mark.asyncio
async def test_diagnostics_includes_detection_result(hass: HomeAssistant, mock_entry):
    await setup_integration(hass, mock_entry, MOCK_RESULT_RAIN)

    diag = await async_get_config_entry_diagnostics(hass, mock_entry)

    detection = diag["detection"]
    assert detection["rain_incoming"] is True
    assert detection["confidence"] == "normal"
    assert detection["frame_count"] == 6
    assert detection["max_approaching_intensity"] == 0.65
    assert detection["arrival_time"] is not None


@pytest.mark.asyncio
async def test_diagnostics_detection_none_when_no_data(hass: HomeAssistant, mock_entry):
    """When coordinator.data is None, detection should be None."""
    await setup_integration(hass, mock_entry, MOCK_RESULT_RAIN)

    # Force coordinator data to None to simulate no data state
    coordinator = hass.data[DOMAIN][mock_entry.entry_id]
    coordinator.data = None

    diag = await async_get_config_entry_diagnostics(hass, mock_entry)

    assert diag["detection"] is None


async def test_diagnostics_redacts_carto_key(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={
        "latitude": -33.701, "longitude": 151.209,
        "lookahead_minutes": 60, "carto_api_key": "private-key",
    }, version=2)
    await setup_integration(hass, entry, MOCK_RESULT_RAIN)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["config"]["carto_api_key"] == "**REDACTED**"
    assert "private-key" not in str(diag)
    assert entry.data["carto_api_key"] == "private-key"
