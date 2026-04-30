"""Derive a home-terminal time zone from a US lat/lng.

We use a coarse longitude-based US timezone heuristic. This is intentionally
simple — for a production app you would use the `timezonefinder` library or
a polygon-based lookup. For US interstate trucking, longitude bands work
well enough because state boundaries roughly follow them.
"""


def tz_for_us_coords(lat: float, lng: float) -> str:
    """Return an IANA tz string based on longitude for the contiguous US.

    Falls back to America/New_York for unrecognized regions.
    """
    # Special cases
    if lat > 60:
        return "America/Anchorage"
    if 18 < lat < 23 and -160 < lng < -154:
        return "Pacific/Honolulu"

    # Contiguous US longitude bands (approximate)
    if lng >= -82.5:
        return "America/New_York"     # Eastern
    if lng >= -97.0:
        return "America/Chicago"      # Central
    if lng >= -114.5:
        return "America/Denver"       # Mountain
    if lng >= -125.0:
        return "America/Los_Angeles"  # Pacific
    return "America/New_York"
