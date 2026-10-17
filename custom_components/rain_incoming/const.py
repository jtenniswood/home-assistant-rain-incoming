DOMAIN = "rain_incoming"

# Config keys (user-facing)
CONF_LOCATION_NAME = "location_name"
CONF_LOOKAHEAD_MINUTES = "lookahead_minutes"
CONF_CARTO_API_KEY = "carto_api_key"
CONF_MAP_STYLE = "map_style"
CONF_FORECAST_CONFIDENCE_ENABLED = "forecast_confidence_enabled"
CONF_SATELLITE_CONFIDENCE_ENABLED = "satellite_confidence_enabled"

# Bypass criteria for the PoP multiplier gate. Both must be met:
# - intensity at or above STRONG_BYPASS_INTENSITY (well above the 0.1 noise floor)
# - track length at or above STRONG_BYPASS_MIN_FRAMES (more than the bare minimum)
# QC still leaves some noise in, so high intensity alone is not enough -
# persistence across frames is the second confidence signal.
STRONG_BYPASS_INTENSITY = 0.2
STRONG_BYPASS_MIN_FRAMES = 3

# Location name display limit (chars). Names longer than this overflow the
# date/time field in the radar image header at all three zoom levels.
MAX_LOCATION_NAME_CHARS = 16

# Maximum number of rain_incoming locations a user can configure.
# RainViewer's free tier rate-limits parallel tile fetching across multiple
# locations. Empirically, 4 locations is the practical maximum without
# degraded performance (5+ second slow-tile-fetch warnings start appearing).
# This limit can be raised when #87 (shared global tile cache) lands and
# reduces per-location fetch cost.
MAX_LOCATIONS = 4

# Lookahead bounds
DEFAULT_LOOKAHEAD_MINUTES = 30
MIN_LOOKAHEAD_MINUTES = 20
MAX_LOOKAHEAD_MINUTES = 60

# System config - not user-configurable
POLL_INTERVAL_SECONDS = 600
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 60
BACKOFF_MULTIPLIER = 2

# Frame thresholds
MIN_FRAMES_NORMAL = 3
MIN_FRAMES_DEGRADED = 2

# Detection thresholds
INTENSITY_THRESHOLD = 0.1
# Lowered from 4 to 1 (2026-05-04) after rain-incoming-backtester sweep on
# 19-day full set: POD +3.6% mean across 7 active locations, with biggest
# gains at coastal/METAR locations (hilo +8.4%, quillayute +7.3%, mobile
# +4.8%) where sea-breeze convection produces small isolated cells. FAR
# rose +24% mean - accepted per user preference of POD over FAR.
MIN_CELL_AREA_PIXELS = 1
MIN_TEMPORAL_FRAMES = 2
MAX_ANGULAR_VARIANCE_RADIANS = 0.5
MAX_STORM_SPEED_KMH = 120.0
PROXIMITY_RADIUS_KM = 5.0

# Radar image config
RADAR_RADII_KM = [64, 128, 256]
RADAR_GIF_FRAME_DURATION_MS = 500

# RainViewer tile config
RAINVIEWER_ZOOM = 7
RAINVIEWER_TILE_SIZE = 256
RAINVIEWER_COLOUR_SCHEME = 6
RAINVIEWER_ANALYSIS_GRID = 2  # fetch (2*N+1)^2 tiles centred on location

# Required attribution for RainViewer tile data per their API terms.
# rainviewer.com/api.html: "We kindly ask you to mention the Rain Viewer API
# as a source of the data on your website with a link: https://www.rainviewer.com/"
# We can't render hyperlinks in a baked GIF, so we include the domain in the text.
RAINVIEWER_ATTRIBUTION = "rainviewer.com"

# NASA GIBS Himawari Band 13 Clean Infrared - cloud presence at zoom 6 (max).
# Used as a confidence signal: no clouds nearby means radar returns are noise.
GIBS_HIMAWARI_URL = (
    "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
    "Himawari_AHI_Band13_Clean_Infrared/default/default/"
    "GoogleMapsCompatible_Level6/{z}/{y}/{x}.png"
)
GIBS_HIMAWARI_ZOOM = 6

# Satellite IR cloud-presence detection parameters (POC-validated).
# Pixels with luminance >= threshold are treated as cloudy. Threshold ~120
# matches the colormap rendering for cold-cloud tops in Band 13. The 50km
# window around a location is wide enough to capture nearby clouds that
# could plausibly produce radar returns at the location itself.
SATELLITE_CHECK_RADIUS_KM = 50
SATELLITE_CLOUD_BRIGHTNESS_THRESHOLD = 120

# A cloud-presence fraction at or above this value within the check window
# means clouds are present and radar returns can be trusted normally. Below
# this, the sky is treated as clear and any radar return is presumed noise -
# the detection threshold is raised to compensate.
SATELLITE_CLOUD_PRESENCE_FRACTION = 0.1
