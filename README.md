# Fuel Route Optimizer

A Django REST API that, given a **start** and **finish** in the USA, computes the
driving route, picks **cost-optimal fuel stops** along it from a CSV of truckstop
prices, respects a **500-mile vehicle range**, and returns the **total fuel cost**
plus the route as GeoJSON and an optional rendered map.

- **Routing:** OSRM public demo server — **exactly one** routing call per request.
- **Fuel-stop optimization:** done entirely **locally** in Python (shapely), against
  stations that were geocoded **once, offline**.
- **Assumptions:** 500-mile max range, 10 miles/gallon, full tank at the start.

---

## Quickstart (clean clone)

Requires **Python 3.12+** (Django 6.0 needs it).

```bash
# 1. Install dependencies
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 2. Create the database schema
python manage.py migrate

# 3. Load + geocode the fuel stations (offline, run once; ~seconds)
python manage.py load_stations

# 4. Run
python manage.py runserver
```

Then POST a route:

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Dallas, TX", "finish": "Chicago, IL"}'
```

Saved sample requests live in `samples/` (`./samples/run_samples.sh` fires them all).

---

## How it works (the one architectural rule)

The fuel-prices CSV has names/addresses/prices but **no coordinates**, and its
`Address` column is highway/exit text (`"I-44, EXIT 283 & US-69"`) that street
geocoders can't resolve. So the design splits cleanly:

- **Offline, once** (`load_stations`): dedupe by truckstop ID (keep the lowest price),
  geocode each **City + State** to a centroid via a local US-cities dataset, and store
  the enriched rows in SQLite. No per-request geocoding, ever.
- **Online, per request**: make **one** OSRM call for the route geometry, then do all
  spatial work locally — a shapely `STRtree` finds stations within ~5 mi of the route,
  each is projected onto the route to get its mile-marker, and a greedy optimizer picks
  the cheapest feasible set of stops.

### Request pipeline
```
geocode start/finish  ->  ONE OSRM route call  ->  candidates near route (STRtree)
                                                ->  optimize fuel stops  ->  JSON / map
```

| Stage | Module |
|-------|--------|
| Endpoint geocoding (Nominatim / raw `lat,lng`) | `routing/services/geocoding.py` |
| The single routing call (cached)               | `routing/services/routing_api.py` |
| Spatial candidate selection                    | `routing/services/station_index.py` |
| Cost-optimal fuel stops                        | `routing/services/optimizer.py` |
| Folium HTML map                                | `routing/services/mapping.py` |
| Offline CSV loader + geocoder                  | `routing/management/commands/load_stations.py` |

---

## API

### `POST /api/route/`

Request — `start`/`finish` accept a place string **or** a raw `"lat,lng"`:
```json
{ "start": "New York, NY", "finish": "Los Angeles, CA" }
```

Response `200` (truncated):
```json
{
  "start":  { "label": "New York, NY", "lat": 40.71, "lng": -74.0 },
  "finish": { "label": "Los Angeles, CA", "lat": 34.05, "lng": -118.24 },
  "total_distance_miles": 2798.4,
  "total_gallons": 279.84,
  "total_fuel_cost_usd": 699.56,
  "fuel_stops": [
    { "name": "...", "address": "...", "lat": 41.1, "lng": -80.6,
      "mile_marker": 404.0, "price_per_gallon": 3.06,
      "gallons_purchased": 38.8, "leg_cost_usd": 118.7 }
  ],
  "route_geojson": { "type": "LineString", "coordinates": [[-74.0, 40.71], "..."] },
  "map_url": "/api/route/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA",
  "assumptions": { "max_range_miles": 500.0, "mpg": 10.0, "start_tank": "full" }
}
```

Short trips that fit within one tank return `fuel_stops: []`, `total_fuel_cost_usd: 0.0`,
and an explanatory `note`.

**Error codes**
| Status | When |
|--------|------|
| `400` | Invalid body, or a start/finish that can't be geocoded |
| `422` | Feasible request but no legal fueling plan (a gap > 500 mi) |
| `502` | The routing API itself failed |

### `GET /api/route/map/?start=...&finish=...`
Returns the rendered Folium HTML map (route polyline + start/finish + fuel-stop
markers). This is the `map_url` from the JSON response.

---

## Assumptions (documented, configurable)

All overridable via `.env` (see `.env.example`) or environment variables:

| Setting | Default | Meaning |
|---------|---------|---------|
| `MAX_RANGE_MILES` | `500` | Max distance on a full tank. |
| `MPG` | `10` | Fuel economy; gallons = miles / MPG. |
| `ASSUME_FULL_START_TANK` | `true` | Vehicle starts with a free full tank (modeled as a virtual origin station priced $0). |
| `ROUTE_BUFFER_DEG` | `0.07` | Corridor half-width (~5 mi) for selecting nearby stations. |
| `OSRM_BASE_URL` | OSRM demo | Routing provider base URL. |

Other modeling notes:
- **Geocoding granularity:** stations are placed at their City+State centroid (within a
  few miles of the true location, well inside the ~5 mi route corridor). The CSV's
  `Address` column is intentionally **not** geocoded.
- **Out-of-USA rows:** a small number of Canadian truckstops in the CSV don't match the
  US-cities dataset and are skipped (logged as a count by `load_stations`).
- **Optimizer:** look-ahead greedy on the classic gas-station problem — at each stop,
  buy just enough to reach a cheaper station ahead, otherwise fill up (or buy exactly
  enough to finish). See the docstring in `optimizer.py`.

---

## Performance

- **Exactly one** external routing call per request (plus geocoding, a separate
  service). Repeat requests for the same pair are served from an in-process route
  cache — verify via the console log line `OSRM routing call (1 network request)`.
- All fuel-stop math is local: an `STRtree` keeps candidate selection fast even with
  thousands of stations.

---

## Tech stack

Python 3.12+ · Django 6.0 + Django REST Framework · SQLite · `shapely` (geometry +
STRtree) · `geopy` (Nominatim, distances) · `requests` (OSRM) · `folium` (map) ·
`python-dotenv`.

External free services: **OSRM** public demo (routing), **Nominatim**/OpenStreetMap
(endpoint geocoding). A fallback routing provider is OpenRouteService's free tier.

## Tests

```bash
python manage.py test routing
```
Covers the routing service (parsing, miles, caching, errors + a live OSRM smoke test),
the station index, the optimizer (hand-verified cases incl. range/infeasibility), and
the API (happy path, no-stop, 400/422/502, map rendering).

## Loader options
```bash
python manage.py load_stations            # load (refuses if table already populated)
python manage.py load_stations --reset     # wipe + reload
python manage.py load_stations --refresh   # ignore the geocoded cache, rebuild it
```
