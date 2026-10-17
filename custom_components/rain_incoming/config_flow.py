from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    LocationSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CARTO_API_KEY,
    CONF_FORECAST_CONFIDENCE_ENABLED,
    CONF_LOCATION_NAME,
    CONF_LOOKAHEAD_MINUTES,
    CONF_MAP_STYLE,
    CONF_SATELLITE_CONFIDENCE_ENABLED,
    DEFAULT_LOOKAHEAD_MINUTES,
    DOMAIN,
    MAX_LOCATION_NAME_CHARS,
    MAX_LOCATIONS,
    MAX_LOOKAHEAD_MINUTES,
    MIN_LOOKAHEAD_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

_VALID_MAP_STYLES = ["voyager", "osm_standard", "osm_dark", "esri_imagery", "dark_matter"]
_DEFAULT_MAP_STYLE = "voyager"

CONF_LOCATION = "location"


def _flatten_location(user_input: dict) -> dict:
    """Extract lat/lon from the LocationSelector dict into flat config keys."""
    if CONF_LOCATION in user_input:
        location = user_input.pop(CONF_LOCATION)
        user_input[CONF_LATITUDE] = location[CONF_LATITUDE]
        user_input[CONF_LONGITUDE] = location[CONF_LONGITUDE]
    return user_input


def _validate_input(user_input: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    lookahead = user_input[CONF_LOOKAHEAD_MINUTES]

    if not (MIN_LOOKAHEAD_MINUTES <= lookahead <= MAX_LOOKAHEAD_MINUTES):
        errors[CONF_LOOKAHEAD_MINUTES] = "invalid_lookahead"

    location_name = user_input.get(CONF_LOCATION_NAME, "")
    if location_name and len(location_name) > MAX_LOCATION_NAME_CHARS:
        errors[CONF_LOCATION_NAME] = "location_name_too_long"

    map_style = user_input.get(CONF_MAP_STYLE, _DEFAULT_MAP_STYLE)
    if map_style not in _VALID_MAP_STYLES:
        errors[CONF_MAP_STYLE] = "invalid_map_style"

    return errors


def _validate_options_input(user_input: dict) -> dict[str, str]:
    """Validate options flow input."""
    errors: dict[str, str] = {}

    lookahead = user_input.get(CONF_LOOKAHEAD_MINUTES)
    if lookahead is not None and not (MIN_LOOKAHEAD_MINUTES <= lookahead <= MAX_LOOKAHEAD_MINUTES):
        errors[CONF_LOOKAHEAD_MINUTES] = "invalid_lookahead"

    location_name = user_input.get(CONF_LOCATION_NAME, "")
    if location_name and len(location_name) > MAX_LOCATION_NAME_CHARS:
        errors[CONF_LOCATION_NAME] = "location_name_too_long"

    map_style = user_input.get(CONF_MAP_STYLE, _DEFAULT_MAP_STYLE)
    if map_style not in _VALID_MAP_STYLES:
        errors[CONF_MAP_STYLE] = "invalid_map_style"

    lat = user_input.get(CONF_LATITUDE)
    if lat is not None and not (-90.0 <= lat <= 90.0):
        errors[CONF_LATITUDE] = "invalid_latitude"

    lon = user_input.get(CONF_LONGITUDE)
    if lon is not None and not (-180.0 <= lon <= 180.0):
        errors[CONF_LONGITUDE] = "invalid_longitude"

    return errors


def _build_title(user_input: dict) -> str:
    location_name = user_input.get(CONF_LOCATION_NAME, "").strip()
    if location_name:
        return f"Rain Incoming - {location_name}"
    lat = user_input[CONF_LATITUDE]
    lon = user_input[CONF_LONGITUDE]
    return f"Rain Incoming ({lat:.2f}, {lon:.2f})"


def _build_schema(
    default_lat: float,
    default_lon: float,
    default_lookahead: int = DEFAULT_LOOKAHEAD_MINUTES,
    default_location_name: str = "",
    default_map_style: str = _DEFAULT_MAP_STYLE,
    default_carto_api_key: str = "",
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_LOCATION,
                default={CONF_LATITUDE: default_lat, CONF_LONGITUDE: default_lon},
            ): LocationSelector(),
            vol.Required(CONF_LOOKAHEAD_MINUTES, default=default_lookahead): vol.Coerce(int),
            vol.Optional(CONF_LOCATION_NAME, default=default_location_name): str,
            vol.Optional(CONF_MAP_STYLE, default=default_map_style): SelectSelector(
                SelectSelectorConfig(
                    options=list(_VALID_MAP_STYLES),
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="map_style",
                )
            ),
            vol.Optional(CONF_CARTO_API_KEY, default=default_carto_api_key): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


def _build_options_schema(
    default_lat: float = 0.0,
    default_lon: float = 0.0,
    default_lookahead: int = DEFAULT_LOOKAHEAD_MINUTES,
    default_location_name: str = "",
    default_map_style: str = _DEFAULT_MAP_STYLE,
    default_forecast_confidence: bool = False,
    default_satellite_confidence: bool = False,
    default_carto_api_key: str = "",
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_LATITUDE, default=default_lat): vol.Coerce(float),
            vol.Required(CONF_LONGITUDE, default=default_lon): vol.Coerce(float),
            vol.Required(CONF_LOOKAHEAD_MINUTES, default=default_lookahead): vol.Coerce(int),
            vol.Optional(CONF_LOCATION_NAME, default=default_location_name): str,
            vol.Optional(CONF_MAP_STYLE, default=default_map_style): SelectSelector(
                SelectSelectorConfig(
                    options=list(_VALID_MAP_STYLES),
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="map_style",
                )
            ),
            vol.Optional(CONF_CARTO_API_KEY, default=default_carto_api_key): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_FORECAST_CONFIDENCE_ENABLED, default=default_forecast_confidence): bool,
            vol.Optional(CONF_SATELLITE_CONFIDENCE_ENABLED, default=default_satellite_confidence): bool,
        }
    )


class RainIncomingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Rain Incoming."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialise flow state."""
        super().__init__()
        self._pending_input: dict | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RainIncomingOptionsFlow(config_entry)

    def _create_entry(self, user_input: dict) -> ConfigFlowResult:
        """Create a config entry from validated, flattened user input."""
        if CONF_MAP_STYLE not in user_input:
            user_input = {**user_input, CONF_MAP_STYLE: _DEFAULT_MAP_STYLE}
        return self.async_create_entry(
            title=_build_title(user_input),
            data=user_input,
        )

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        existing = self.hass.config_entries.async_entries(DOMAIN)
        enabled = [e for e in existing if e.disabled_by is None]
        if len(enabled) >= MAX_LOCATIONS:
            return self.async_abort(reason="too_many_locations")

        errors: dict[str, str] = {}

        if user_input is not None:
            if CONF_CARTO_API_KEY in user_input:
                user_input[CONF_CARTO_API_KEY] = user_input[CONF_CARTO_API_KEY].strip()
            _flatten_location(user_input)
            errors = _validate_input(user_input)
            if not errors:
                lat = user_input[CONF_LATITUDE]
                lon = user_input[CONF_LONGITUDE]
                await self.async_set_unique_id(f"{lat:.4f}_{lon:.4f}")
                self._abort_if_unique_id_configured()

                coverage = await self._check_coverage(lat, lon)
                if coverage is False:
                    self._pending_input = user_input
                    return await self.async_step_confirm_no_coverage()
                return self._create_entry(user_input)

        schema = _build_schema(
            default_lat=user_input.get(CONF_LATITUDE, self.hass.config.latitude) if user_input else self.hass.config.latitude,
            default_lon=user_input.get(CONF_LONGITUDE, self.hass.config.longitude) if user_input else self.hass.config.longitude,
            default_lookahead=user_input.get(CONF_LOOKAHEAD_MINUTES, DEFAULT_LOOKAHEAD_MINUTES) if user_input else DEFAULT_LOOKAHEAD_MINUTES,
            default_location_name=user_input.get(CONF_LOCATION_NAME, "") if user_input else "",
            default_map_style=user_input.get(CONF_MAP_STYLE, _DEFAULT_MAP_STYLE) if user_input else _DEFAULT_MAP_STYLE,
            default_carto_api_key=user_input.get(CONF_CARTO_API_KEY, "") if user_input else "",
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_confirm_no_coverage(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Ask user to confirm setup when no radar coverage was detected."""
        if user_input is not None:
            return self._create_entry(self._pending_input)

        return self.async_show_form(step_id="confirm_no_coverage")

    async def _check_coverage(self, lat: float, lon: float) -> bool | None:
        """Probe RainViewer for radar coverage at the given coordinates.

        Returns True if coverage detected, False if not, None if the
        check could not be completed (API error - fail open).
        """
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        from .providers.rainviewer import check_coverage

        try:
            session = async_get_clientsession(self.hass)
            return await check_coverage(lat, lon, session=session)
        except Exception:
            _LOGGER.warning(
                "Could not verify RainViewer coverage at (%.4f, %.4f) - "
                "proceeding with setup anyway",
                lat, lon,
            )
            return None


def _merge_options_into_data(existing_data: dict, user_input: dict) -> dict:
    """Return a copy of existing_data with any keys present in user_input overwritten."""
    _EDITABLE_OPTION_KEYS = (
        CONF_LATITUDE,
        CONF_LONGITUDE,
        CONF_LOCATION_NAME,
        CONF_LOOKAHEAD_MINUTES,
        CONF_MAP_STYLE,
        CONF_CARTO_API_KEY,
        CONF_FORECAST_CONFIDENCE_ENABLED,
        CONF_SATELLITE_CONFIDENCE_ENABLED,
    )
    merged = dict(existing_data)
    for key in _EDITABLE_OPTION_KEYS:
        if key in user_input:
            merged[key] = user_input[key]
    return merged


def _resolve_options_schema_defaults(
    current_data: dict, user_input: dict | None
) -> vol.Schema:
    """Return schema keyword args for the options form.

    On first render use persisted entry.data; on re-render after a validation
    failure restore what the user typed so they don't have to retype everything.
    """
    if user_input is not None:
        # Re-render after validation failure: restore what the user typed.
        return _build_options_schema(
            default_lat=user_input.get(CONF_LATITUDE, current_data.get(CONF_LATITUDE, 0.0)),
            default_lon=user_input.get(CONF_LONGITUDE, current_data.get(CONF_LONGITUDE, 0.0)),
            default_lookahead=user_input.get(CONF_LOOKAHEAD_MINUTES, DEFAULT_LOOKAHEAD_MINUTES),
            default_location_name=user_input.get(CONF_LOCATION_NAME, ""),
            default_map_style=user_input.get(CONF_MAP_STYLE, _DEFAULT_MAP_STYLE),
            default_carto_api_key=user_input.get(CONF_CARTO_API_KEY, current_data.get(CONF_CARTO_API_KEY, "")),
            default_forecast_confidence=user_input.get(CONF_FORECAST_CONFIDENCE_ENABLED, current_data.get(CONF_FORECAST_CONFIDENCE_ENABLED, False)),
            default_satellite_confidence=user_input.get(CONF_SATELLITE_CONFIDENCE_ENABLED, current_data.get(CONF_SATELLITE_CONFIDENCE_ENABLED, False)),
        )
    # First render: use persisted values from entry.data.
    return _build_options_schema(
        default_lat=current_data.get(CONF_LATITUDE, 0.0),
        default_lon=current_data.get(CONF_LONGITUDE, 0.0),
        default_lookahead=current_data.get(CONF_LOOKAHEAD_MINUTES, DEFAULT_LOOKAHEAD_MINUTES),
        default_location_name=current_data.get(CONF_LOCATION_NAME, ""),
        default_map_style=current_data.get(CONF_MAP_STYLE, _DEFAULT_MAP_STYLE),
        default_carto_api_key=current_data.get(CONF_CARTO_API_KEY, ""),
        default_forecast_confidence=current_data.get(CONF_FORECAST_CONFIDENCE_ENABLED, False),
        default_satellite_confidence=current_data.get(CONF_SATELLITE_CONFIDENCE_ENABLED, False),
    )


class RainIncomingOptionsFlow(OptionsFlow):
    """Options flow: change map_style, location_name, and lookahead post-install.

    Latitude and longitude are install-time properties and are NOT editable here.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if CONF_CARTO_API_KEY in user_input:
                user_input[CONF_CARTO_API_KEY] = user_input[CONF_CARTO_API_KEY].strip()
            errors = _validate_options_input(user_input)
            if not errors:
                return self._apply_options(user_input)

        schema = _resolve_options_schema_defaults(self._config_entry.data, user_input)
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )

    def _apply_options(self, user_input: dict) -> ConfigFlowResult:
        """Persist validated options into entry.data, update title, and schedule reload."""
        merged_data = _merge_options_into_data(self._config_entry.data, user_input)
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data=merged_data,
            title=_build_title(merged_data),
        )
        # Trigger reload so the coordinator picks up the new style immediately.
        self.hass.config_entries.async_schedule_reload(self._config_entry.entry_id)
        # Return empty options - all values are stored in entry.data.
        # Avoids dual storage where the same values live in both
        # data and options, creating a divergence risk.
        return self.async_create_entry(data={})
