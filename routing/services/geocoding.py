"""Resolve start/finish to coordinates via raw 'lat,lng' or Nominatim."""

from django.conf import settings
from geopy.exc import GeocoderServiceError, GeocoderTimedOut, GeocoderUnavailable
from geopy.geocoders import Nominatim

_geolocator = None
_cache: dict[str, tuple] = {}


class GeocodingError(Exception):
    """Raised when a location string cannot be resolved to coordinates."""


def _get_geolocator():
    global _geolocator
    if _geolocator is None:
        _geolocator = Nominatim(user_agent=settings.NOMINATIM_USER_AGENT)
    return _geolocator


def _try_parse_latlng(value):
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None
    return lat, lng


def resolve_location(value):
    """Resolve a place string or 'lat,lng' to (lat, lng, label).

    Raises GeocodingError on empty input or an un-geocodable place.
    """
    if value is None or not str(value).strip():
        raise GeocodingError("Location is empty.")
    value = str(value).strip()

    raw = _try_parse_latlng(value)
    if raw is not None:
        return raw[0], raw[1], value

    if value in _cache:
        return _cache[value]

    try:
        location = _get_geolocator().geocode(
            value, country_codes="us", timeout=10
        )
    except (GeocoderTimedOut, GeocoderUnavailable, GeocoderServiceError) as exc:
        raise GeocodingError(f"Geocoding service error: {exc}") from exc

    if location is None:
        raise GeocodingError(f"Could not geocode location: {value!r}")

    result = (location.latitude, location.longitude, value)
    _cache[value] = result
    return result


def clear_cache():
    _cache.clear()
