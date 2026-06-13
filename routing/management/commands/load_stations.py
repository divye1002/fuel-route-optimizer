"""Offline loader: dedupe the fuel-prices CSV, geocode by City+State centroid via a
local US-cities dataset, and bulk-insert FuelStation rows.

    python manage.py load_stations            # load
    python manage.py load_stations --reset     # wipe table first, then load
    python manage.py load_stations --refresh   # ignore the geocoded cache, rebuild it

Dedupe is by OPIS Truckstop ID (keep the lowest price). The CSV `Address` column is
highway/exit text and is never geocoded. Unmatched (city, state) pairs are skipped and
counted. Results are cached to data/stations_geocoded.csv for instant re-runs.
"""

import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import FuelStation

DATA_DIR = Path(settings.BASE_DIR) / "data"
FUEL_CSV = DATA_DIR / "fuel-prices-for-be-assessment.csv"
CITIES_CSV = DATA_DIR / "us-cities.csv"
GEOCODED_CACHE = DATA_DIR / "stations_geocoded.csv"

CACHE_FIELDS = [
    "name", "address", "city", "state", "price_per_gallon", "latitude", "longitude",
]


class Command(BaseCommand):
    help = "Load and geocode fuel stations from the assessment CSV into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset", action="store_true",
            help="Delete all existing FuelStation rows before loading.",
        )
        parser.add_argument(
            "--refresh", action="store_true",
            help="Ignore data/stations_geocoded.csv and rebuild it from source.",
        )

    def handle(self, *args, **options):
        if not FUEL_CSV.exists():
            raise CommandError(f"Missing fuel CSV: {FUEL_CSV}")

        if GEOCODED_CACHE.exists() and not options["refresh"]:
            self.stdout.write(f"Using cached geocoded stations: {GEOCODED_CACHE}")
            records, skipped = self._read_cache(), 0
        else:
            records, skipped = self._build_records()
            self._write_cache(records)
            self.stdout.write(f"Wrote geocoded cache: {GEOCODED_CACHE}")

        self._load_db(records, reset=options["reset"])
        self.stdout.write(self.style.SUCCESS(
            f"Loaded {len(records)} stations"
            + (f"; skipped {skipped} (city/state not geocodable)" if skipped else "")
        ))

    # ----- source parse + geocode -------------------------------------------------

    def _build_records(self):
        cities = self._load_cities()
        self.stdout.write(f"Loaded {len(cities)} city/state coordinates.")

        # Dedupe by OPIS id, keeping the lowest retail price per id.
        best = {}  # opis_id -> row dict
        total = 0
        with FUEL_CSV.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                total += 1
                opis = (row.get("OPIS Truckstop ID") or "").strip()
                price = self._parse_price(row.get("Retail Price"))
                if price is None:
                    continue
                if opis not in best or price < best[opis]["_price"]:
                    best[opis] = {
                        "name": (row.get("Truckstop Name") or "").strip(),
                        "address": (row.get("Address") or "").strip(),
                        "city": (row.get("City") or "").strip(),
                        "state": (row.get("State") or "").strip().upper(),
                        "_price": price,
                    }
        self.stdout.write(f"Read {total} rows -> {len(best)} unique stations (deduped by OPIS id).")

        records, skipped = [], 0
        for r in best.values():
            coord = cities.get((r["city"].upper(), r["state"]))
            if coord is None:
                skipped += 1
                continue
            records.append({
                "name": r["name"],
                "address": r["address"],
                "city": r["city"],
                "state": r["state"],
                "price_per_gallon": r["_price"],
                "latitude": coord[0],
                "longitude": coord[1],
            })
        self.stdout.write(f"Geocoded {len(records)} stations; {skipped} unmatched.")
        return records, skipped

    def _load_cities(self):
        if not CITIES_CSV.exists():
            raise CommandError(
                f"Missing city reference: {CITIES_CSV} "
                "(expected columns: city,state,lat,lng)."
            )
        cities = {}
        with CITIES_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["city"].strip().upper(), row["state"].strip().upper())
                try:
                    cities[key] = (float(row["lat"]), float(row["lng"]))
                except (TypeError, ValueError):
                    continue
        return cities

    @staticmethod
    def _parse_price(raw):
        if raw is None:
            return None
        s = raw.strip().lstrip("$").replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None

    # ----- cache ------------------------------------------------------------------

    def _write_cache(self, records):
        with GEOCODED_CACHE.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CACHE_FIELDS)
            w.writeheader()
            w.writerows(records)

    def _read_cache(self):
        records = []
        with GEOCODED_CACHE.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                records.append({
                    "name": row["name"],
                    "address": row["address"],
                    "city": row["city"],
                    "state": row["state"],
                    "price_per_gallon": float(row["price_per_gallon"]),
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                })
        return records

    # ----- DB ---------------------------------------------------------------------

    @transaction.atomic
    def _load_db(self, records, reset):
        if reset:
            deleted, _ = FuelStation.objects.all().delete()
            self.stdout.write(f"Reset: deleted {deleted} existing rows.")
        elif FuelStation.objects.exists():
            raise CommandError(
                "FuelStation table is not empty. Re-run with --reset to reload."
            )
        FuelStation.objects.bulk_create(
            [FuelStation(**r) for r in records], batch_size=1000
        )
