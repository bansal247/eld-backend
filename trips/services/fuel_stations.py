"""
Fetch fuel/gas stations near a route using the Overpass API (OpenStreetMap data).

Returns stations sorted by their mile marker along the route geometry so the
HOS scheduler can stop at a real station instead of an arbitrary mileage mark.
Falls back to an empty list on any error; the scheduler then uses the hard
1000-mile rule as before.
"""
import logging
from math import asin, cos, radians, sin, sqrt
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SAMPLE_INTERVAL_MILES = 100.0  # query around a point every 100 mi along the route
SEARCH_RADIUS_METERS = 12875   # 8 miles in meters (wider to compensate for sparser samples)
MAX_DEVIATION_MILES = 5.0      # discard stations farther than 5 mi from the route


# ---------- Haversine (local copy — avoids importing from hos_scheduler) ----------

def _hav(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r1, r2, r3, r4 = map(radians, [lat1, lng1, lat2, lng2])
    dlat, dlng = r3 - r1, r4 - r2
    h = sin(dlat / 2) ** 2 + cos(r1) * cos(r3) * sin(dlng / 2) ** 2
    return 2 * 3958.7613 * asin(sqrt(h))


# ---------- Public API ----------

def get_fuel_stations_on_leg(geometry: List[List[float]]) -> List[dict]:
    """
    Query Overpass for fuel/gas stations near the route geometry.

    Args:
        geometry: list of [lng, lat] pairs (GeoJSON / ORS format)

    Returns:
        List of {mile_marker, lat, lng, name} dicts sorted by mile_marker.
        Empty list on any error.
    """
    if not geometry or len(geometry) < 2:
        return []

    sample_pts = _sample_geometry(geometry, SAMPLE_INTERVAL_MILES)
    if not sample_pts:
        return []

    union_clauses = "\n".join(
        f'node["amenity"="fuel"](around:{SEARCH_RADIUS_METERS},{lat:.5f},{lng:.5f});'
        for lat, lng in sample_pts
    )
    query = f"[out:json][timeout:15];\n(\n{union_clauses}\n);\nout body;"

    try:
        resp = requests.get(
            OVERPASS_URL,
            params={"data": query},
            headers={"User-Agent": "eld-planner/1.0 (trip planning tool)"},
            timeout=18,
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
    except Exception as exc:
        logger.warning("Overpass API unavailable (%s) — using mileage-based fuel rule", exc)
        return []

    return _map_stations_to_leg(geometry, elements)


# ---------- Internals ----------

def _sample_geometry(geometry: List[List[float]], interval: float) -> List[tuple]:
    """Return (lat, lng) sample points along the geometry every `interval` miles."""
    points: List[tuple] = []
    accumulated = 0.0
    next_sample = 0.0

    # Always include the start
    points.append((geometry[0][1], geometry[0][0]))
    next_sample = interval

    for i in range(len(geometry) - 1):
        lng1, lat1 = geometry[i]
        lng2, lat2 = geometry[i + 1]
        seg_len = _hav(lat1, lng1, lat2, lng2)

        while next_sample <= accumulated + seg_len + 1e-9:
            frac = (next_sample - accumulated) / seg_len if seg_len > 1e-9 else 0.0
            frac = max(0.0, min(1.0, frac))
            pt = (lat1 + (lat2 - lat1) * frac, lng1 + (lng2 - lng1) * frac)
            if not points or pt != points[-1]:
                points.append(pt)
            next_sample += interval

        accumulated += seg_len

    # Always include the end
    end = (geometry[-1][1], geometry[-1][0])
    if not points or points[-1] != end:
        points.append(end)

    return points


def _map_stations_to_leg(
    geometry: List[List[float]], elements: List[dict]
) -> List[dict]:
    """
    Map OSM nodes to mile markers along the route.

    For each station, find the nearest geometry vertex; if it is within
    MAX_DEVIATION_MILES of the route, record its cumulative mile marker.
    """
    if not elements:
        return []

    # Pre-compute cumulative mile markers for each vertex
    cum: List[float] = [0.0]
    for i in range(len(geometry) - 1):
        lng1, lat1 = geometry[i]
        lng2, lat2 = geometry[i + 1]
        cum.append(cum[-1] + _hav(lat1, lng1, lat2, lng2))

    seen_ids: set = set()
    results: List[dict] = []

    for el in elements:
        el_id = el.get("id")
        if el_id in seen_ids:
            continue
        seen_ids.add(el_id)

        s_lat = el["lat"]
        s_lng = el["lon"]

        # Find the nearest geometry vertex
        best_idx = min(
            range(len(geometry)),
            key=lambda i: _hav(s_lat, s_lng, geometry[i][1], geometry[i][0]),
        )
        dist = _hav(s_lat, s_lng, geometry[best_idx][1], geometry[best_idx][0])

        if dist <= MAX_DEVIATION_MILES:
            tags = el.get("tags", {})
            name = tags.get("name") or tags.get("brand") or tags.get("operator") or "Fuel Station"
            results.append(
                {
                    "mile_marker": cum[best_idx],
                    "lat": s_lat,
                    "lng": s_lng,
                    "name": name,
                }
            )

    return sorted(results, key=lambda s: s["mile_marker"])
