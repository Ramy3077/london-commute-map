"""Settings for the commute map build. Edit the values below to rebuild for
your own destination. API keys are read from environment variables, never
stored in the repo."""

import os
from datetime import date, timedelta

# Free key from https://api-portal.tfl.gov.uk (required for the build).
TFL_APP_KEY = os.environ.get("TFL_APP_KEY", "")
# Free key from https://openrouteservice.org (optional: only used for walking
# times, which are not shown on the map).
ORS_API_KEY = os.environ.get("ORS_API_KEY", "")

# The destination every journey is timed to.
DESTINATION = {"lat": 51.5178, "lng": -0.0823, "name": "Liverpool Street station"}

# Area to cover. Zone 1-2 core plus the main direct-line corridors.
BBOX = {"north": 51.61, "south": 51.43, "west": -0.22, "east": 0.055}

# H3 resolution 8 is roughly 0.7 km^2 per hex.
H3_RES = 8

# Reference commute: arrive at the destination at 09:00 on a weekday.
# TfL rejects dates more than 7 days in the past, so default to the next Monday.
# Set an explicit "YYYYMMDD" string to pin it.
_today = date.today()
COMMUTE_DATE = (_today + timedelta(days=(7 - _today.weekday()) % 7 or 7)).strftime("%Y%m%d")
COMMUTE_TIME = "0900"
