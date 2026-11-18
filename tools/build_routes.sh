#!/usr/bin/env bash
# Candidate routes for routeSampler on the Valencia network:
# data/valencia/candidate.rou.xml.gz (~70k routes, minutes of duarouter). They only
# depend on the network, so they are computed once, not on every simulation.
#   tools/build_routes.sh [net] [out]
set -euo pipefail
NET=${1:-data/valencia/valencia.net.xml.gz}
OUT=${2:-data/valencia/candidate.rou.xml.gz}
SUMO_HOME=${SUMO_HOME:-$(python -c 'import sumo, os; print(os.path.dirname(sumo.__file__))')}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
python "$SUMO_HOME/tools/randomTrips.py" -n "$NET" -r "$TMP/routes.rou.xml" -o "$TMP/trips.trips.xml" \
  -l -L --fringe-factor 5 -p 0.05 --min-distance 500 --seed 42
gzip -9 -c "$TMP/routes.rou.xml" > "$OUT"
ls -la "$OUT"
