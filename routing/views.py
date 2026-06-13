from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.serializers import RouteRequestSerializer
from routing.services.geocoding import GeocodingError, resolve_location
from routing.services.mapping import build_map
from routing.services.optimizer import InfeasibleRoute, optimize
from routing.services.routing_api import RoutingError, get_route
from routing.services.station_index import candidates_near_route


def run_pipeline(start_in, finish_in):
    """Returns (start, finish, route, opt); propagates the service exceptions."""
    start = resolve_location(start_in)
    finish = resolve_location(finish_in)
    route = get_route((start[0], start[1]), (finish[0], finish[1]))
    candidates = candidates_near_route(route.coordinates, route.total_distance_miles)
    opt = optimize(candidates, route.total_distance_miles)
    return start, finish, route, opt


def _build_payload(start, finish, route, opt, map_url):
    total_miles = route.total_distance_miles
    fuel_stops = []
    for fs in opt.fuel_stops:
        station = fs.candidate.station
        fuel_stops.append({
            "name": station.name,
            "address": station.address,
            "lat": fs.candidate.lat,
            "lng": fs.candidate.lng,
            "mile_marker": round(fs.mile_marker, 1),
            "price_per_gallon": round(fs.price_per_gallon, 3),
            "gallons_purchased": round(fs.gallons_purchased, 2),
            "leg_cost_usd": round(fs.leg_cost_usd, 2),
        })

    payload = {
        "start": {"label": start[2], "lat": start[0], "lng": start[1]},
        "finish": {"label": finish[2], "lat": finish[0], "lng": finish[1]},
        "total_distance_miles": round(total_miles, 1),
        "total_gallons": round(total_miles / settings.MPG, 2),
        "total_fuel_cost_usd": round(opt.total_fuel_cost_usd, 2),
        "fuel_stops": fuel_stops,
        "route_geojson": route.geometry,
        "map_url": map_url,
        "assumptions": {
            "max_range_miles": settings.MAX_RANGE_MILES,
            "mpg": settings.MPG,
            "start_tank": "full" if settings.ASSUME_FULL_START_TANK else "empty",
        },
    }
    if not fuel_stops:
        payload["note"] = "No fuel stop required: the finish is within range of the start."
    return payload


class RouteAPIView(APIView):
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        start_in = serializer.validated_data["start"]
        finish_in = serializer.validated_data["finish"]

        try:
            start, finish, route, opt = run_pipeline(start_in, finish_in)
        except GeocodingError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except RoutingError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except InfeasibleRoute as exc:
            return Response(
                {"error": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

        map_url = "/api/route/map/?" + urlencode({"start": start_in, "finish": finish_in})
        payload = _build_payload(start, finish, route, opt, map_url)
        return Response(payload, status=status.HTTP_200_OK)


class RouteMapView(APIView):
    def get(self, request):
        start_in = (request.query_params.get("start") or "").strip()
        finish_in = (request.query_params.get("finish") or "").strip()
        if not start_in or not finish_in:
            return HttpResponse(
                "Provide both ?start= and ?finish= query parameters.", status=400
            )

        try:
            start, finish, route, opt = run_pipeline(start_in, finish_in)
        except GeocodingError as exc:
            return HttpResponse(f"Geocoding error: {exc}", status=400)
        except RoutingError as exc:
            return HttpResponse(f"Routing error: {exc}", status=502)
        except InfeasibleRoute as exc:
            return HttpResponse(f"Infeasible route: {exc}", status=422)

        html = build_map(route.coordinates, opt.fuel_stops, start, finish)
        return HttpResponse(html)
