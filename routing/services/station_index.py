"""Memoized shapely STRtree to select fuel stations near a route, locally."""

from dataclasses import dataclass

from django.conf import settings
from shapely import STRtree
from shapely.geometry import LineString, Point

from routing.models import FuelStation

_tree: STRtree | None = None
_stations: list[FuelStation] | None = None


@dataclass
class StationOnRoute:
    station: FuelStation
    distance_from_start_miles: float
    price_per_gallon: float

    @property
    def lat(self) -> float:
        return self.station.latitude

    @property
    def lng(self) -> float:
        return self.station.longitude


def build_index(force: bool = False):
    global _tree, _stations
    if _tree is not None and not force:
        return
    _stations = list(
        FuelStation.objects.exclude(latitude__isnull=True)
        .exclude(longitude__isnull=True)
    )
    points = [Point(s.longitude, s.latitude) for s in _stations]
    _tree = STRtree(points)


def reset_index():
    global _tree, _stations
    _tree = None
    _stations = None


def candidates_near_route(route_coords, total_route_miles) -> list[StationOnRoute]:
    """Stations within the route corridor, projected to miles-from-start, sorted."""
    build_index()
    if _tree is None or not _stations or len(route_coords) < 2:
        return []

    line = LineString(route_coords)  # coords are (lng, lat) == (x, y)
    if line.length == 0:
        return []

    corridor = line.buffer(settings.ROUTE_BUFFER_DEG)
    idx = _tree.query(corridor, predicate="intersects")

    miles_per_unit = total_route_miles / line.length
    found: list[StationOnRoute] = []
    for i in idx:
        station = _stations[i]
        along = line.project(Point(station.longitude, station.latitude))
        found.append(
            StationOnRoute(
                station=station,
                distance_from_start_miles=along * miles_per_unit,
                price_per_gallon=station.price_per_gallon,
            )
        )

    return _dedupe_by_position(found)


def _dedupe_by_position(candidates, position_resolution_miles: float = 0.1):
    """Collapse co-located stations (shared city centroid) to the cheapest, then sort."""
    cheapest_at = {}
    for c in candidates:
        bucket = round(c.distance_from_start_miles / position_resolution_miles)
        existing = cheapest_at.get(bucket)
        if existing is None or c.price_per_gallon < existing.price_per_gallon:
            cheapest_at[bucket] = c
    return sorted(cheapest_at.values(), key=lambda c: c.distance_from_start_miles)
