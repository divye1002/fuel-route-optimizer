from dataclasses import dataclass, field

from django.conf import settings

EPS = 1e-9


class InfeasibleRoute(Exception):
    """No legal fueling plan exists (a >range gap, or the finish is unreachable)."""


@dataclass
class FuelStop:
    candidate: object            # the StationOnRoute we bought at
    mile_marker: float
    price_per_gallon: float
    gallons_purchased: float
    leg_cost_usd: float


@dataclass
class OptimizerResult:
    total_fuel_cost_usd: float
    total_gallons_purchased: float
    fuel_stops: list = field(default_factory=list)


def optimize(
    candidates,
    total_route_miles,
    *,
    max_range_miles=None,
    mpg=None,
    assume_full_start_tank=None,
):
    """Compute the cost-optimal fuel stops along a route.

    `candidates` is a list of StationOnRoute (must expose
    `distance_from_start_miles` and `price_per_gallon`); order is not assumed.
    Returns an OptimizerResult. Raises InfeasibleRoute on an impossible route.
    """
    RANGE = settings.MAX_RANGE_MILES if max_range_miles is None else max_range_miles
    MPG = settings.MPG if mpg is None else mpg
    full = (
        settings.ASSUME_FULL_START_TANK
        if assume_full_start_tank is None
        else assume_full_start_tank
    )
    L = float(total_route_miles)

    # Virtual origin (free tank) + the real stations, sorted by position.
    # Each entry: (position_miles, price, candidate_or_None).
    stops = [(0.0, 0.0, None)]
    stops += [
        (float(c.distance_from_start_miles), float(c.price_per_gallon), c)
        for c in candidates
    ]
    stops.sort(key=lambda s: s[0])
    n = len(stops)

    pos = 0.0
    fuel = RANGE if full else 0.0          # miles of range currently in the tank
    cost = 0.0
    gallons_bought = 0.0
    fuel_stops: list[FuelStop] = []

    def buy_at(i, miles):
        """Purchase `miles` of range at station i; record it if non-trivial."""
        nonlocal cost, gallons_bought
        if miles <= EPS:
            return
        gallons = miles / MPG
        spend = gallons * stops[i][1]
        cost += spend
        gallons_bought += gallons
        candidate = stops[i][2]
        if candidate is not None:          # skip the virtual origin
            fuel_stops.append(
                FuelStop(
                    candidate=candidate,
                    mile_marker=stops[i][0],
                    price_per_gallon=stops[i][1],
                    gallons_purchased=gallons,
                    leg_cost_usd=spend,
                )
            )

    i = 0  # index of the station we are currently parked at
    while True:
        # Already enough fuel in the tank to coast to the finish?
        if pos + fuel >= L - EPS:
            break

        current_price = stops[i][1]
        # Stations strictly ahead and reachable on a full tank from here.
        window = [
            j for j in range(i + 1, n)
            if stops[j][0] > pos + EPS and stops[j][0] <= pos + RANGE + EPS
        ]

        # First station in the window cheaper than where we stand.
        cheaper = next(
            (j for j in window if stops[j][1] < current_price - EPS), None
        )

        if cheaper is not None:
            # Buy only enough to reach the cheaper station; refuel there.
            target = stops[cheaper][0]
            need = target - pos
            buy_at(i, need - fuel)
            fuel = max(fuel, need)         # we now hold at least `need`
            fuel -= need
            pos = target
            i = cheaper
        elif pos + RANGE >= L - EPS:
            # No cheaper station ahead and the finish is reachable: buy exactly
            # enough to finish (filling fully would strand paid-for fuel).
            need = L - pos
            buy_at(i, need - fuel)
            pos = L
            break
        elif window:
            # No cheaper station and can't finish yet: fill fully at this (cheapest
            # reachable) price, then jump to the cheapest station in range.
            buy_at(i, RANGE - fuel)
            fuel = RANGE
            target_j = min(window, key=lambda j: (stops[j][1], -stops[j][0]))
            fuel -= stops[target_j][0] - pos
            pos = stops[target_j][0]
            i = target_j
        else:
            # Nothing reachable ahead and we can't finish -> stranded.
            raise InfeasibleRoute(
                f"No fuel station within {RANGE:.0f} mi of mile {pos:.1f} "
                f"(and the finish at mile {L:.1f} is out of range)."
            )

    return OptimizerResult(
        total_fuel_cost_usd=cost,
        total_gallons_purchased=gallons_bought,
        fuel_stops=fuel_stops,
    )
