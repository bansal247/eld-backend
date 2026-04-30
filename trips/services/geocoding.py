"""Geocoding via Nominatim (OpenStreetMap).

Free, no API key, but rate-limited to 1 request/second. We aggressively cache
results to avoid hammering the public service.
"""
import logging
import time

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "ELDTripPlanner/1.0 (educational project)"
CACHE_TTL = 60 * 60 * 24 * 7  # 1 week

# Simple in-memory rate limiter (1 req/sec per Nominatim policy)
_last_request_time = 0.0


def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_request_time = time.time()


def geocode(query: str, limit: int = 5) -> list:
    """Search Nominatim for a free-text query.

    Returns a list of {display_name, lat, lng} dicts (up to `limit`).
    """
    query = (query or "").strip()
    if len(query) < 3:
        return []

    cache_key = f"geocode:{query.lower()}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    _rate_limit()
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={'q': query, 'format': 'json', 'limit': limit, 'addressdetails': 0},
            headers={'User-Agent': USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Nominatim error: %s", e)
        return []

    results = []
    for hit in r.json():
        try:
            results.append({
                'display_name': hit['display_name'],
                'lat': float(hit['lat']),
                'lng': float(hit['lon']),
            })
        except (KeyError, ValueError):
            continue

    cache.set(cache_key, results, CACHE_TTL)
    return results
