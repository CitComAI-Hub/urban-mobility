#!/usr/bin/env bash
# Builds the SUMO network of Valencia from OpenStreetMap (ODbL), as used by the
# CitCom data space job: data/valencia/valencia.net.xml.gz.
#   tools/build_network.sh [out_dir]
# Needs curl and netconvert (pip install eclipse-sumo==1.25.0). Overpass mirrors
# are tried in order; the public ones are often overloaded.
set -euo pipefail
OUT=${1:-data/valencia}
# Bounding box (south, west, north, east): the area of the Ajuntament's traffic
# measurement points, with a margin.
S=39.43075660183345 W=-0.4320342963669762 N=39.51217197505403 E=-0.31956796747849237
ROADS='motorway|trunk|primary|secondary|tertiary|unclassified|residential|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link|living_street|service'
Q="[out:xml][timeout:180];(way[\"highway\"~\"^($ROADS)\$\"]($S,$W,$N,$E););(._;>;);out body;"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
for api in https://overpass-api.de/api/interpreter https://overpass.private.coffee/api/interpreter https://overpass.kumi.systems/api/interpreter; do
  if curl -sf -m 300 -A "urban-mobility-network-build/1.0" --data-urlencode "data=$Q" -o "$TMP/valencia.osm" "$api" \
     && grep -q '<way ' "$TMP/valencia.osm"; then
    echo "OSM data from $api"; break
  fi
done
grep -q '<way ' "$TMP/valencia.osm" || { echo "No Overpass mirror answered" >&2; exit 1; }
mkdir -p "$OUT"
netconvert --osm-files "$TMP/valencia.osm" -o "$OUT/valencia.net.xml.gz" \
  --geometry.remove --ramps.guess --junctions.join \
  --tls.guess-signals --tls.discard-simple --tls.join --roundabouts.guess \
  --remove-edges.isolated --keep-edges.components 1 \
  --output.street-names --output.original-names
ls -la "$OUT/valencia.net.xml.gz"
