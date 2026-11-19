"""Zones where a local twin is better determined: the traffic entering them is counted.

    python tools/calibration/find_zones.py <net> <counts.csv> [out.json]

Maps the detectors to edges as the job does, then slides square windows (1–3 km) over
the network. For each window: entry edges = edges that cross its boundary inwards
(from-node outside, to-node inside), weighted by lanes (residential and service streets
count 0.3). An entry is covered when a detector sits on it or on the edge right after it.
Score = covered weight / total weight. Prints the best non-overlapping windows with at
least MIN_DETECTORS detectors inside.
"""
import json
import math
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(__file__))
import evaluate  # noqa: E402
import sumolib  # noqa: E402
from config import settings  # noqa: E402
from sumo_controller import SumoController  # noqa: E402

MIN_DETECTORS = int(os.getenv("MIN_DETECTORS", "25"))
MINOR = {"residential", "service", "living_street", "unclassified", "track"}


def detector_edges(net_file, sample):
    detectors, flows = evaluate.inputs(sample)
    settings.WORK_DIR = tempfile.mkdtemp(prefix="sumo-zones-")
    c = SumoController(name="z", network_url=None, network_file=net_file, detectors=detectors, flow_data=flows,
                       flow_interval_minutes=60, simulation_datetime="2026-01-01T00:00:00", meso_simulation=True,
                       upload_results=False, cleanup=False, candidate_routes_file="x")
    c._SumoController__create_domain()
    c._SumoController__process_detectors()
    edges = set()
    for _, el in ET.iterparse(c.detectors_file):
        if el.tag == "e1Detector":
            edges.add(el.get("lane").rsplit("_", 1)[0])
    os.system(f"rm -rf {settings.WORK_DIR}")
    return edges


def main():
    net_file, sample, *rest = sys.argv[1:]
    out = rest[0] if rest else None
    net = sumolib.net.readNet(net_file)
    D = detector_edges(net_file, sample)
    edges = [e for e in net.getEdges() if e.allows("passenger") and not e.isSpecial()]
    nodes = {n.getID(): n.getCoord() for n in net.getNodes()}
    det_xy = [net.getEdge(e).getShape()[len(net.getEdge(e).getShape()) // 2] for e in D]
    (xmin, ymin), (xmax, ymax) = net.getBBoxXY()
    cands = []
    for size in (1000, 1500, 2000, 2500, 3000):
        step = 250
        x = xmin
        while x + size <= xmax:
            y = ymin
            while y + size <= ymax:
                inside = lambda p: x <= p[0] <= x + size and y <= p[1] <= y + size  # noqa: E731
                ndet = sum(1 for p in det_xy if inside(p))
                if ndet >= MIN_DETECTORS:
                    tot = cov = 0.0
                    for e in edges:
                        if not inside(nodes[e.getFromNode().getID()]) and inside(nodes[e.getToNode().getID()]):
                            w = e.getLaneNumber() * (0.3 if (e.getType() or "").split(".")[-1] in MINOR else 1.0)
                            tot += w
                            nxt = {o.getID() for o in e.getOutgoing()}
                            if e.getID() in D or nxt & D:
                                cov += w
                    if tot:
                        cands.append({"size": size, "x": x, "y": y, "detectors": ndet,
                                      "entry_weight": round(tot, 1), "covered": round(cov / tot, 3)})
                y += step
            x += step
    cands.sort(key=lambda c: (c["covered"], c["detectors"]), reverse=True)
    best = []
    for c in cands:
        if all(c["x"] + c["size"] <= b["x"] or b["x"] + b["size"] <= c["x"] or
               c["y"] + c["size"] <= b["y"] or b["y"] + b["size"] <= c["y"] for b in best):
            lon0, lat0 = net.convertXY2LonLat(c["x"], c["y"])
            lon1, lat1 = net.convertXY2LonLat(c["x"] + c["size"], c["y"] + c["size"])
            c["lonlat"] = [round(lon0, 5), round(lat0, 5), round(lon1, 5), round(lat1, 5)]
            best.append(c)
        if len(best) == 8:
            break
    for b in best:
        print(json.dumps(b))
    if out:
        json.dump(best, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
