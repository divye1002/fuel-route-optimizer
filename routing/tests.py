from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase

from routing.models import FuelStation
from routing.services import routing_api, station_index
from routing.services.geocoding import GeocodingError
from routing.services.routing_api import (
    METERS_TO_MILES,
    RouteResult,
    RoutingError,
    get_route,
)
from routing.services.station_index import candidates_near_route
from routing.services.optimizer import InfeasibleRoute, optimize


class _Cand:
    """Minimal stand-in for StationOnRoute in optimizer tests."""

    def __init__(self, miles, price):
        self.distance_from_start_miles = miles
        self.price_per_gallon = price

# A minimal but realistic OSRM /route/v1 response.
_OSRM_OK = {
    "code": "Ok",
    "routes": [
        {
            "distance": 100000.0,  # meters
            "geometry": {
                "type": "LineString",
                "coordinates": [[-74.0, 40.71], [-75.0, 40.0], [-118.24, 34.05]],
            },
        }
    ],
}


def _mock_response(json_payload, status=200):
    resp = MagicMock()
    resp.json.return_value = json_payload
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


class RoutingApiTests(SimpleTestCase):
    def setUp(self):
        routing_api.clear_cache()

    @patch("routing.services.routing_api.requests.get")
    def test_parses_coords_and_converts_miles(self, mock_get):
        mock_get.return_value = _mock_response(_OSRM_OK)

        result = get_route((40.71, -74.0), (34.05, -118.24))

        self.assertEqual(result.coordinates[0], [-74.0, 40.71])
        self.assertEqual(result.coordinates[-1], [-118.24, 34.05])
        self.assertEqual(result.total_distance_meters, 100000.0)
        self.assertAlmostEqual(
            result.total_distance_miles, 100000.0 * METERS_TO_MILES, places=4
        )
        self.assertFalse(result.from_cache)
        # Confirm we asked OSRM for geojson geometry, full overview.
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["geometries"], "geojson")
        self.assertEqual(kwargs["params"]["overview"], "full")

    @patch("routing.services.routing_api.requests.get")
    def test_url_uses_lng_lat_order(self, mock_get):
        mock_get.return_value = _mock_response(_OSRM_OK)
        get_route((40.71, -74.0), (34.05, -118.24))
        url = mock_get.call_args[0][0]
        # OSRM wants {lng},{lat};{lng},{lat}
        self.assertIn("-74.0,40.71;-118.24,34.05", url)

    @patch("routing.services.routing_api.requests.get")
    def test_cache_yields_single_network_call(self, mock_get):
        mock_get.return_value = _mock_response(_OSRM_OK)

        first = get_route((40.71, -74.0), (34.05, -118.24))
        second = get_route((40.71, -74.0), (34.05, -118.24))

        self.assertEqual(mock_get.call_count, 1)  # second served from cache
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(first.coordinates, second.coordinates)

    @patch("routing.services.routing_api.requests.get")
    def test_http_failure_raises_routing_error(self, mock_get):
        mock_get.return_value = _mock_response({}, status=500)
        with self.assertRaises(RoutingError):
            get_route((40.71, -74.0), (34.05, -118.24))

    @patch("routing.services.routing_api.requests.get")
    def test_network_exception_raises_routing_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("boom")
        with self.assertRaises(RoutingError):
            get_route((40.71, -74.0), (34.05, -118.24))

    @patch("routing.services.routing_api.requests.get")
    def test_no_route_raises_routing_error(self, mock_get):
        mock_get.return_value = _mock_response({"code": "NoRoute", "routes": []})
        with self.assertRaises(RoutingError):
            get_route((40.71, -74.0), (34.05, -118.24))

    @patch("routing.services.routing_api.requests.get")
    def test_empty_geometry_raises_routing_error(self, mock_get):
        payload = {"code": "Ok", "routes": [{"distance": 1.0, "geometry": {"coordinates": []}}]}
        mock_get.return_value = _mock_response(payload)
        with self.assertRaises(RoutingError):
            get_route((40.71, -74.0), (34.05, -118.24))


class RoutingApiLiveTests(SimpleTestCase):
    """Hits the real OSRM demo server. Skips gracefully if the network is down."""

    def setUp(self):
        routing_api.clear_cache()

    def test_live_known_routes(self):
        pairs = [
            # (start lat,lng), (finish lat,lng), rough expected miles
            ((40.7128, -74.0060), (34.0522, -118.2437), 2400),   # NYC -> LA
            ((41.8781, -87.6298), (39.7392, -104.9903), 800),    # Chicago -> Denver
        ]
        for start, finish, expected_miles in pairs:
            try:
                result = get_route(start, finish)
            except RoutingError as exc:
                self.skipTest(f"OSRM unreachable: {exc}")
            self.assertGreater(len(result.coordinates), 2)
            self.assertGreater(result.total_distance_miles, 0)
            # Sanity: within a generous band of the real-world distance.
            self.assertGreater(result.total_distance_miles, expected_miles * 0.6)
            self.assertLess(result.total_distance_miles, expected_miles * 1.6)


class StationIndexTests(TestCase):
    """A horizontal route from (lng=0,lat=0) to (lng=10,lat=0), length 10 deg,
    pretending to be 1000 miles, so mileage == lng * 100."""

    ROUTE = [[0.0, 0.0], [10.0, 0.0]]
    TOTAL_MILES = 1000.0

    def _make(self, name, lng, lat, price):
        return FuelStation.objects.create(
            name=name, city="X", state="ZZ",
            price_per_gallon=price, latitude=lat, longitude=lng,
        )

    def setUp(self):
        station_index.reset_index()

    def tearDown(self):
        station_index.reset_index()

    def test_projects_to_mileage_and_sorts(self):
        # Within the corridor (lat within ~0.07 deg of the line).
        self._make("A", 2.0, 0.01, 3.50)
        self._make("B", 8.0, -0.02, 3.10)
        self._make("C", 5.0, 0.0, 3.30)

        out = candidates_near_route(self.ROUTE, self.TOTAL_MILES)

        self.assertEqual([c.station.name for c in out], ["A", "C", "B"])  # sorted by mile
        miles = [c.distance_from_start_miles for c in out]
        self.assertAlmostEqual(miles[0], 200.0, delta=2)
        self.assertAlmostEqual(miles[1], 500.0, delta=2)
        self.assertAlmostEqual(miles[2], 800.0, delta=2)

    def test_excludes_stations_outside_corridor(self):
        self._make("near", 5.0, 0.0, 3.0)
        self._make("far", 5.0, 5.0, 1.0)  # 5 deg off the line, way outside buffer

        out = candidates_near_route(self.ROUTE, self.TOTAL_MILES)

        self.assertEqual([c.station.name for c in out], ["near"])

    def test_dedupes_colocated_keeping_cheaper(self):
        # Same city centroid -> identical projection; keep the cheaper price.
        self._make("pricey", 5.0, 0.0, 4.00)
        self._make("cheap", 5.0, 0.0, 2.90)

        out = candidates_near_route(self.ROUTE, self.TOTAL_MILES)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].station.name, "cheap")
        self.assertEqual(out[0].price_per_gallon, 2.90)

    def test_empty_when_no_stations(self):
        self.assertEqual(candidates_near_route(self.ROUTE, self.TOTAL_MILES), [])


class OptimizerTests(SimpleTestCase):
    KW = dict(max_range_miles=500, mpg=10, assume_full_start_tank=True)

    def test_short_route_needs_no_fuel(self):
        # Finish within the free starting tank -> zero cost, no stops.
        result = optimize([_Cand(200, 3.0)], total_route_miles=300, **self.KW)
        self.assertEqual(result.total_fuel_cost_usd, 0.0)
        self.assertEqual(result.fuel_stops, [])

    def test_worked_example_buys_to_finish_not_full(self):
        # L=1000; A@400=$3, B@700=$2, C@900=$4.
        # Optimal: buy 20 gal at A ($60) to reach B, then exactly 30 gal at B ($60).
        # C is never used. Naive "fill fully at B" would cost $160; correct is $120.
        cands = [_Cand(400, 3.0), _Cand(700, 2.0), _Cand(900, 4.0)]
        result = optimize(cands, total_route_miles=1000, **self.KW)

        self.assertAlmostEqual(result.total_fuel_cost_usd, 120.0, places=6)
        self.assertAlmostEqual(result.total_gallons_purchased, 50.0, places=6)
        self.assertEqual(len(result.fuel_stops), 2)
        a, b = result.fuel_stops
        self.assertAlmostEqual(a.mile_marker, 400)
        self.assertAlmostEqual(a.gallons_purchased, 20.0)
        self.assertAlmostEqual(a.leg_cost_usd, 60.0)
        self.assertAlmostEqual(b.mile_marker, 700)
        self.assertAlmostEqual(b.gallons_purchased, 30.0)
        self.assertAlmostEqual(b.leg_cost_usd, 60.0)

    def test_buys_exact_amount_to_finish_at_single_station(self):
        # L=600; only A@400=$3. Need 100 mi (10 gal) beyond the free tank.
        result = optimize([_Cand(400, 3.0)], total_route_miles=600, **self.KW)
        self.assertAlmostEqual(result.total_fuel_cost_usd, 30.0, places=6)
        self.assertEqual(len(result.fuel_stops), 1)
        self.assertAlmostEqual(result.fuel_stops[0].gallons_purchased, 10.0)

    def test_prefers_cheaper_station_buying_minimum_to_reach_it(self):
        # Cheaper station ahead within range: buy only enough at the dear one to reach it.
        # L=1000; A@300=$5 (dear), B@600=$1 (cheap).
        cands = [_Cand(300, 5.0), _Cand(600, 1.0)]
        result = optimize(cands, total_route_miles=1000, **self.KW)
        a, b = result.fuel_stops
        # From origin (free 500 mi) we can reach B@600? No (500<600), so we must buy
        # 100 mi at A@300 to reach B, then finish from B.
        self.assertAlmostEqual(a.mile_marker, 300)
        self.assertAlmostEqual(a.gallons_purchased, 10.0)   # 100 mi shortfall to reach B
        self.assertAlmostEqual(a.leg_cost_usd, 50.0)
        self.assertAlmostEqual(b.mile_marker, 600)
        self.assertAlmostEqual(b.gallons_purchased, 40.0)   # 400 mi to finish
        self.assertAlmostEqual(b.leg_cost_usd, 40.0)
        self.assertAlmostEqual(result.total_fuel_cost_usd, 90.0)

    def test_infeasible_gap_raises(self):
        # Station at 400, finish at 1200: from 400 nothing within 500 mi -> stranded.
        with self.assertRaises(InfeasibleRoute):
            optimize([_Cand(400, 3.0)], total_route_miles=1200, **self.KW)

    def test_infeasible_when_first_station_too_far(self):
        # Free tank reaches 500; first/only station at 700 -> can't even start buying.
        with self.assertRaises(InfeasibleRoute):
            optimize([_Cand(700, 3.0)], total_route_miles=1500, **self.KW)


# A horizontal route; with total_distance_miles=600 over 10 deg, mile == lng * 60.
_ROUTE = RouteResult(
    coordinates=[[0.0, 0.0], [10.0, 0.0]],
    total_distance_meters=600 / METERS_TO_MILES,
    total_distance_miles=600.0,
    geometry={"type": "LineString", "coordinates": [[0.0, 0.0], [10.0, 0.0]]},
)


class RouteAPITests(TestCase):
    URL = "/api/route/"

    def setUp(self):
        station_index.reset_index()

    def tearDown(self):
        station_index.reset_index()

    @patch("routing.views.get_route", return_value=_ROUTE)
    @patch("routing.views.resolve_location")
    def test_happy_path(self, mock_geo, mock_route):
        mock_geo.side_effect = [
            (0.0, 0.0, "Start"),
            (0.0, 10.0, "Finish"),
        ]
        # One station at lng=5 -> mile 300, price $3.00.
        FuelStation.objects.create(
            name="Mid", address="Hwy 1", city="M", state="ZZ",
            price_per_gallon=3.0, latitude=0.0, longitude=5.0,
        )

        resp = self.client.post(
            self.URL, {"start": "Start", "finish": "Finish"},
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total_distance_miles"], 600.0)
        self.assertEqual(data["total_gallons"], 60.0)
        self.assertAlmostEqual(data["total_fuel_cost_usd"], 30.0, places=2)
        self.assertEqual(len(data["fuel_stops"]), 1)
        stop = data["fuel_stops"][0]
        self.assertEqual(stop["name"], "Mid")
        self.assertAlmostEqual(stop["mile_marker"], 300, delta=2)
        self.assertAlmostEqual(stop["gallons_purchased"], 10.0, places=2)
        self.assertEqual(data["assumptions"]["max_range_miles"], 500)
        self.assertEqual(data["route_geojson"]["type"], "LineString")
        # Exactly one routing call.
        self.assertEqual(mock_route.call_count, 1)

    @patch("routing.views.get_route", return_value=_ROUTE)
    @patch("routing.views.resolve_location")
    def test_short_route_no_stop_required(self, mock_geo, mock_route):
        mock_geo.side_effect = [(0.0, 0.0, "A"), (0.0, 10.0, "B")]
        # 600 mi > 500 range, but make it short by... use a station-free 400-mi route.
        mock_route.return_value = RouteResult(
            coordinates=[[0.0, 0.0], [10.0, 0.0]],
            total_distance_meters=400 / METERS_TO_MILES,
            total_distance_miles=400.0,
            geometry={"type": "LineString", "coordinates": [[0.0, 0.0], [10.0, 0.0]]},
        )
        resp = self.client.post(
            self.URL, {"start": "A", "finish": "B"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["fuel_stops"], [])
        self.assertEqual(data["total_fuel_cost_usd"], 0.0)
        self.assertIn("note", data)

    def test_missing_field_is_400(self):
        resp = self.client.post(
            self.URL, {"start": "A"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    @patch("routing.views.resolve_location", side_effect=GeocodingError("nope"))
    def test_ungeocodable_is_400(self, _mock):
        resp = self.client.post(
            self.URL, {"start": "Nowhere", "finish": "B"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    @patch("routing.views.get_route", side_effect=RoutingError("down"))
    @patch("routing.views.resolve_location")
    def test_routing_failure_is_502(self, mock_geo, _mock_route):
        mock_geo.side_effect = [(0.0, 0.0, "A"), (0.0, 10.0, "B")]
        resp = self.client.post(
            self.URL, {"start": "A", "finish": "B"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 502)

    @patch("routing.views.get_route")
    @patch("routing.views.resolve_location")
    def test_infeasible_is_422(self, mock_geo, mock_route):
        mock_geo.side_effect = [(0.0, 0.0, "A"), (0.0, 10.0, "B")]
        # 1000-mi route with zero stations -> can't get past mile 500.
        mock_route.return_value = RouteResult(
            coordinates=[[0.0, 0.0], [10.0, 0.0]],
            total_distance_meters=1000 / METERS_TO_MILES,
            total_distance_miles=1000.0,
            geometry={"type": "LineString", "coordinates": [[0.0, 0.0], [10.0, 0.0]]},
        )
        resp = self.client.post(
            self.URL, {"start": "A", "finish": "B"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 422)


class RouteMapTests(TestCase):
    URL = "/api/route/map/"

    def setUp(self):
        station_index.reset_index()

    def tearDown(self):
        station_index.reset_index()

    @patch("routing.views.get_route", return_value=_ROUTE)
    @patch("routing.views.resolve_location")
    def test_map_renders_html(self, mock_geo, mock_route):
        mock_geo.side_effect = [(0.0, 0.0, "Start"), (0.0, 10.0, "Finish")]
        FuelStation.objects.create(
            name="MidStop", address="Hwy 1", city="M", state="ZZ",
            price_per_gallon=3.0, latitude=0.0, longitude=5.0,
        )
        resp = self.client.get(self.URL, {"start": "Start", "finish": "Finish"})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("folium", body.lower())
        self.assertIn("MidStop", body)        # fuel-stop marker present
        self.assertEqual(mock_route.call_count, 1)

    def test_map_missing_params_is_400(self):
        resp = self.client.get(self.URL, {"start": "A"})
        self.assertEqual(resp.status_code, 400)
