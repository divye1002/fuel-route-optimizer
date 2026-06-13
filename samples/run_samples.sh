#!/usr/bin/env bash
# Fire the saved sample requests at a running dev server.
#   1) python manage.py runserver
#   2) ./samples/run_samples.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
DIR="$(dirname "$0")"

for f in long_trip short_trip raw_coords; do
  echo "===== $f ====="
  curl -s -X POST "$BASE/api/route/" \
    -H "Content-Type: application/json" \
    -d @"$DIR/$f.json"
  echo
done

echo "===== map (open in a browser) ====="
echo "$BASE/api/route/map/?start=Dallas,+TX&finish=Chicago,+IL"
