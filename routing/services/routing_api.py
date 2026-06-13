"""Single OSRM routing call per request, with an in-process result cache."""

import logging
from dataclasses import dataclass, field, replace

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

METERS_TO_MILES = 0.000621371

_CACHE_PRECISION = 4  # ~11 m; collapses near-identical requests onto one call
_route_cache: dict[tuple, "RouteResult"] = {}


class RoutingError(Exception):
    """Raised when the routing API fails or returns no usable route."""


@dataclass
class RouteResult:
    coordinates: list[list[float]]      # [[lng, lat], ...] in route order
    total_distance_meters: float
    total_distance_miles: float
    geometry: dict = field(default_factory=dict)
    from_cache: bool = False


def _cache_key(start, finish):
    return (
        round(start[0], _CACHE_PRECISION), round(start[1], _CACHE_PRECISION),
        round(finish[0], _CACHE_PRECISION), round(finish[1], _CACHE_PRECISION),
    )


def get_route(start_latlng, finish_latlng) -> RouteResult:
    """Driving route between two (lat, lng) points. One OSRM call on a cache miss."""
    key = _cache_key(start_latlng, finish_latlng)
    if key in _route_cache:
        logger.info("OSRM route cache HIT (no network call)")
        return replace(_route_cache[key], from_cache=True)

    start_lat, start_lng = start_latlng
    finish_lat, finish_lng = finish_latlng
    base = settings.OSRM_BASE_URL.rstrip("/")
    # OSRM expects {lng},{lat};{lng},{lat}
    url = f"{base}/route/v1/driving/{start_lng},{start_lat};{finish_lng},{finish_lat}"
    params = {"overview": "full", "geometries": "geojson"}

    logger.info("OSRM routing call (1 network request): %s", url)
    try:
        resp = requests.get(url, params=params, timeout=settings.ROUTING_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise RoutingError(f"Routing request failed: {exc}") from exc
    except ValueError as exc:
        raise RoutingError("Routing API returned malformed JSON.") from exc

    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RoutingError(f"Routing API could not find a route (code={payload.get('code')!r}).")

    route = payload["routes"][0]
    geometry = route.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if not coordinates:
        raise RoutingError("Routing API returned an empty geometry.")

    meters = float(route.get("distance", 0.0))
    result = RouteResult(
        coordinates=coordinates,
        total_distance_meters=meters,
        total_distance_miles=meters * METERS_TO_MILES,
        geometry=geometry,
    )
    _route_cache[key] = result
    return result


def clear_cache():
    _route_cache.clear()
