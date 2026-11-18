"""SUMO network of Valencia: OpenStreetMap + the Ajuntament's data read through the data
space. Replaces tools/build_network.sh.

    python tools/network/build_network.py [--osm valencia.osm] [--out data/valencia/valencia.net.xml.gz]

1. Roads from OpenStreetMap (ODbL, Overpass; --osm to reuse a download), with the
   lane access of OSM (bus lanes: --osm.lane-access).
2. Traffic signals: the Ajuntament's controllers, Smart Data Models Device read
   through the data space (offering traffic-signals of townhall-vlc; DS_* variables
   of ds-consumer-auth, as the job). A junction gets a traffic light if a controller
   is within SIGNAL_RADIUS m, and loses the one OSM gave it if none is within
   UNSET_RADIUS m. The controllers carry no timings: SUMO's default programs.
3. Speed limits: OSM's maxspeed where tagged. Elsewhere, on urban roads, the general
   limits of the Spanish traffic rules (Reglamento General de Circulación, art. 50,
   as amended by RD 970/2020): 30 km/h with one lane per direction, 50 km/h with
   two or more. Living streets and motorways keep netconvert's defaults.

Prints what it changed. Needs netconvert (eclipse-sumo) and, for 2, the offering.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import requests  # noqa: E402
import sumolib  # noqa: E402

# Area of the Ajuntament's traffic measurement points, with a margin (S, W, N, E)
BBOX = (39.43075660183345, -0.4320342963669762, 39.51217197505403, -0.31956796747849237)
ROADS = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|motorway_link|trunk_link|"
         "primary_link|secondary_link|tertiary_link|living_street|service")
OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
NETCONVERT_OPTIONS = [
    "--geometry.remove", "--ramps.guess", "--junctions.join",
    "--tls.join", "--roundabouts.guess",
    "--remove-edges.isolated", "--keep-edges.components", "1",
    "--output.street-names", "--output.original-names",
    "--osm.lane-access",
]
URBAN_ROADS = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
               "unclassified", "residential"}
SIGNAL_RADIUS = 40.0
UNSET_RADIUS = 60.0
DEVICE_CONTEXT = "https://raw.githubusercontent.com/smart-data-models/dataModel.Device/refs/heads/master/context.jsonld"


def download_osm(path: str) -> None:
    s, w, n, e = BBOX
    query = f'[out:xml][timeout:180];(way["highway"~"^({ROADS})$"]({s},{w},{n},{e}););(._;>;);out body;'
    for api in OVERPASS:
        try:
            req = urllib.request.Request(api, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers={"User-Agent": "urban-mobility-network-build/1.0"})
            with urllib.request.urlopen(req, timeout=300) as resp, open(path, "wb") as out:
                shutil.copyfileobj(resp, out)
            if "<way " in open(path, encoding="utf-8", errors="ignore").read(2_000_000):
                print("OSM data from", api)
                return
        except OSError as error:
            print("Overpass", api, "failed:", error)
    sys.exit("No Overpass mirror answered")


def traffic_signals(source_url: str) -> list[tuple[float, float, str]]:
    """(lon, lat, id) of the controllers, through the data space."""
    if os.getenv("DS_ISSUER_URL"):
        from ds_consumer_auth import data_space_session

        session = data_space_session()
    else:  # a broker without access control (local tests)
        session = requests.Session()
    link = f'<{DEVICE_CONTEXT}>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
    found, offset = [], 0
    while True:
        r = session.get(f"{source_url.rstrip('/')}/ngsi-ld/v1/entities",
                        params={"type": "Device", "options": "keyValues", "limit": 1000, "offset": offset},
                        headers={"Link": link, "Accept": "application/json"}, timeout=60)
        r.raise_for_status()
        page = r.json()
        for e in page:
            # NGSI-LD returns a one-element array as the element itself
            props = e.get("controlledProperty")
            props = props if isinstance(props, list) else [props]
            loc = e.get("location") or {}
            if "trafficFlow" in props and loc.get("type") == "Point":
                found.append((loc["coordinates"][0], loc["coordinates"][1], e["id"]))
        if len(page) < 1000:
            return found
        offset += 1000


def osm_tagged_speed_ways(osm_file: str) -> set[str]:
    tagged = set()
    for _, el in ET.iterparse(osm_file):
        if el.tag == "way":
            if any(t.get("k") == "maxspeed" for t in el.findall("tag")):
                tagged.add(el.get("id"))
            el.clear()
    return tagged


def run(cmd: list[str]) -> None:
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode:
        output = (result.stdout + result.stderr).splitlines()
        errors = [line for line in output if not line.startswith("Warning")]
        sys.exit(f"{cmd[0]} failed (exit {result.returncode}):\n" + "\n".join(errors[-20:]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--osm", help="OSM file to reuse instead of downloading")
    ap.add_argument("--out", default="data/valencia/valencia.net.xml.gz")
    ap.add_argument("--keep-tmp", action="store_true", help="keep the intermediate files")
    ap.add_argument("--source-url", default=os.getenv("TRAFFIC_SOURCE_URL",
                                                       "https://apisix-townhall-vlc.citcom-vrain-dataspace.eu"))
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="netbuild-")
    try:
        osm = args.osm or os.path.join(tmp, "valencia.osm")
        if not args.osm:
            download_osm(osm)
        base = os.path.join(tmp, "base.net.xml")
        run(["netconvert", "--osm-files", osm, "-o", base, *NETCONVERT_OPTIONS])
        net = sumolib.net.readNet(base, withPrograms=True)

        # Traffic signals from the Ajuntament's controllers
        signals = traffic_signals(args.source_url)
        print(f"Traffic signal controllers read through the data space: {len(signals)}")
        candidates = [n for n in net.getNodes() if len(n.getIncoming()) + len(n.getOutgoing()) >= 3]
        points = [net.convertLonLat2XY(lon, lat) for lon, lat, _ in signals]
        to_set, matched = set(), 0
        for x, y in points:
            best = min(candidates, key=lambda n: math.dist(n.getCoord(), (x, y)), default=None)
            if best is not None and math.dist(best.getCoord(), (x, y)) <= SIGNAL_RADIUS:
                to_set.add(best.getID())
                matched += 1
        to_unset = set()
        for node in net.getNodes():
            if node.getType().startswith("traffic_light") and node.getID() not in to_set:
                if all(math.dist(node.getCoord(), p) > UNSET_RADIUS for p in points):
                    to_unset.add(node.getID())
        had = sum(1 for n in net.getNodes() if n.getType().startswith("traffic_light"))
        print(f"Junctions with traffic lights: OSM {had}; controllers matched to a junction {matched}/{len(points)}; "
              f"set {len(to_set)}, removed {len(to_unset)} (no controller within {UNSET_RADIUS:.0f} m)")

        # Speed limits where OSM has none
        tagged = osm_tagged_speed_ways(osm)
        patch = os.path.join(tmp, "speeds.edg.xml")
        changed = 0
        with open(patch, "w") as f:
            f.write("<edges>\n")
            for edge in net.getEdges():
                road = edge.getType().replace("highway.", "")
                if road not in URBAN_ROADS:
                    continue
                orig = edge.getLanes()[0].getParam("origId", "") or ""
                if not orig or any(w in tagged for w in orig.split()):
                    continue
                speed = 30 / 3.6 if edge.getLaneNumber() == 1 else 50 / 3.6
                if abs(edge.getSpeed() - speed) > 0.01:
                    f.write(f'  <edge id="{edge.getID()}" speed="{speed:.2f}"/>\n')
                    changed += 1
            f.write("</edges>\n")
        print(f"Speed limit from the traffic rules on {changed} edges without maxspeed in OSM")

        # Traffic light changes as a node patch: --tls.set crashes netconvert 1.25 on
        # some junctions of this network (segmentation fault)
        nodes = os.path.join(tmp, "signals.nod.xml")
        with open(nodes, "w") as f:
            f.write("<nodes>\n")
            for node_id in sorted(to_set):
                f.write(f'  <node id="{node_id}" type="traffic_light"/>\n')
            for node_id in sorted(to_unset):
                f.write(f'  <node id="{node_id}" type="priority"/>\n')
            f.write("</nodes>\n")
        options = ["-s", base, "-n", nodes, "-e", patch, "-o", args.out,
                   "--output.street-names", "--output.original-names"]
        run(["netconvert", *options])
        final = sumolib.net.readNet(args.out)
        print(f"Network: {len(final.getEdges())} edges, "
              f"{sum(1 for n in final.getNodes() if n.getType().startswith('traffic_light'))} junctions with traffic lights "
              f"-> {args.out}")
    finally:
        if args.keep_tmp:
            print("Intermediate files in", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
