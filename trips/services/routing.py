"""Routing via OpenRouteService (free tier, ~2000 req/day).

Uses the driving-hgv (heavy goods vehicle) profile for truck-appropriate routes.
Falls back to a great-circle estimate if no API key is configured or ORS errors.
"""
import logging
from math import asin, cos, radians, sin, sqrt

import requests
from django.conf import settings

from .hos_scheduler import Leg

logger = logging.getLogger(__name__)

ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-hgv/geojson"
METERS_PER_MILE = 1609.34


def _haversine_miles(lat1, lng1, lat2, lng2) -> float:
    rlat1, rlng1, rlat2, rlng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat, dlng = rlat2 - rlat1, rlng2 - rlng1
    h = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlng / 2) ** 2
    return 2 * 3958.7613 * asin(sqrt(h))


AVG_SPEED_MPH = 60.0  # scheduling assumption shown to user in the UI


def _fallback_leg(lat1, lng1, lat2, lng2) -> Leg:
    """Great-circle distance × 1.3 to approximate road distance, 60 mph average."""
    great_circle = _haversine_miles(lat1, lng1, lat2, lng2)
    road_miles = great_circle * 1.3
    return Leg(
        distance_miles=road_miles,
        duration_hours=road_miles / AVG_SPEED_MPH,
        geometry=[[lng1, lat1], [lng2, lat2]],
    )


def get_route(lat1: float, lng1: float, lat2: float, lng2: float) -> Leg:
    """Get a truck route between two points. Returns a Leg.

    If OpenRouteService is not configured or fails, returns a fallback estimate
    based on great-circle distance.
    """
    # Identical points -> empty leg
    if abs(lat1 - lat2) < 1e-6 and abs(lng1 - lng2) < 1e-6:
        return Leg(distance_miles=0.0, duration_hours=0.0, geometry=[[lng1, lat1]])

    api_key = getattr(settings, 'ORS_API_KEY', None)
    if not api_key:
        logger.warning("ORS_API_KEY not set — using great-circle fallback")
        return _fallback_leg(lat1, lng1, lat2, lng2)

    try:
        r = requests.post(
            ORS_URL,
            headers={
                'Authorization': api_key,
                'Content-Type': 'application/json',
            },
            json={
                'coordinates': [[lng1, lat1], [lng2, lat2]],
                'units': 'mi',
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        feature = data['features'][0]
        summary = feature['properties']['summary']
        coords = feature['geometry']['coordinates']  # list of [lng, lat]

        # ORS returns distance in meters by default. With units='mi' it's miles.
        # Distance and duration units depend on the API plan/profile, so be defensive:
        distance_mi = summary['distance']
        # Heuristic: if distance > 50000, it's almost certainly meters not miles
        if distance_mi > 50000:
            distance_mi = distance_mi / METERS_PER_MILE
        # Use fixed AVG_SPEED_MPH for scheduling; ORS travel times vary by road type
        return Leg(
            distance_miles=distance_mi,
            duration_hours=distance_mi / AVG_SPEED_MPH,
            geometry=coords,
        )
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        logger.warning("ORS error (%s) — using fallback", e)
        return _fallback_leg(lat1, lng1, lat2, lng2)
