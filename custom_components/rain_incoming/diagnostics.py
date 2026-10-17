from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .const import CONF_CARTO_API_KEY, DOMAIN
from .coordinator import RainDetectorCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    coordinator: RainDetectorCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Anonymize location (round to 1 decimal - enough for region debugging)
    data = async_redact_data(dict(entry.data), {CONF_CARTO_API_KEY})
    if "latitude" in data:
        data["latitude"] = round(data["latitude"], 1)
    if "longitude" in data:
        data["longitude"] = round(data["longitude"], 1)

    detection = None
    if coordinator.data is not None:
        detection = {
            "rain_incoming": coordinator.data.rain_incoming,
            "arrival_time": str(coordinator.data.arrival_time) if coordinator.data.arrival_time else None,
            "confidence": coordinator.data.confidence.value,
            "frame_count": coordinator.data.frame_count,
            "max_approaching_intensity": coordinator.data.max_approaching_intensity,
        }

    return {
        "config": data,
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_update_success_time": str(coordinator.last_update_success_time) if coordinator.last_update_success_time else None,
            "latest_frame_path": coordinator.latest_frame_path,
            "last_rain_nearby_time": str(coordinator.last_rain_nearby_time) if coordinator.last_rain_nearby_time else None,
            "consecutive_failures": coordinator.consecutive_failures,
            "update_interval_seconds": coordinator.update_interval.total_seconds() if coordinator.update_interval else None,
        },
        "detection": detection,
    }
