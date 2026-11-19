"""Capacity feedback for routeSampler: cap the start edges that cannot insert their demand.

    python tools/calibration/capacity_iter.py <net> <counts.csv> [iterations]

routeSampler only matches the counted edges, so it can load an uncounted entry edge with
more vehicles than it can let in (they never depart and the counts downstream are lost).
Each iteration simulates, finds the start edges where more than THRESHOLD of the vehicles
did not depart, and adds them to routeSampler's input as extra "counts" equal to what they
actually inserted per hour (never raised again). Prints the fit of every iteration.
Env: as evaluate.py (CANDIDATE_ROUTES_FILE, SUMO_ARGS, WARMUP_HOURS); THRESHOLD (0.2).
"""
import collections
import json
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(__file__))
import evaluate  # noqa: E402
from sdm_output import detector_fit  # noqa: E402
from sumo_controller import SumoController  # noqa: E402

REAL = set()  # edges with real counts (never capped)

THRESHOLD = float(os.getenv("THRESHOLD", "0.2"))
CAPS = {}  # edge -> veh/h
TRIPINFO = os.path.join(os.getenv("TMPDIR", "/tmp"), "capacity_iter.tripinfo.xml")

_orig_edgedata = SumoController._SumoController__generate_edgedata


def _edgedata_with_caps(self):
    _orig_edgedata(self)
    tree = ET.parse(self.edgedata_file)
    for interval in tree.getroot().iter("interval"):
        have = {e.get("id") for e in interval.findall("edge")}
        REAL.update(have)
        for edge, cap in CAPS.items():
            if edge not in have:
                ET.SubElement(interval, "edge", id=edge, passenger=f"{cap:.0f}")
    tree.write(self.edgedata_file)


SumoController._SumoController__generate_edgedata = _edgedata_with_caps


def start_edges(vehicles_file):
    start = {}
    for _, el in ET.iterparse(vehicles_file):
        if el.tag == "vehicle":
            start[el.get("id")] = el.find("route").get("edges").split()[0]
            el.clear()
    return start


def main():
    net, sample, *rest = sys.argv[1:]
    iterations = int(rest[0]) if rest else 4
    base = json.loads(os.getenv("SUMO_ARGS"))
    os.environ["SUMO_ARGS"] = json.dumps(base + ["--tripinfo-output", TRIPINFO,
                                                 "--tripinfo-output.write-undeparted", "true"])
    os.environ["KEEP_WORK_DIR"] = "1"
    routes = os.environ["CANDIDATE_ROUTES_FILE"]
    detectors, flows = evaluate.inputs(sample)
    hours = evaluate.WARMUP_HOURS + 1
    for it in range(iterations + 1):
        evaluate.run(net, routes, detectors, flows)
        work = os.path.join(evaluate.settings.WORK_DIR, "cal")
        # the fit only over the real counts: drop the caps from the observed file
        tree = ET.parse(os.path.join(work, "edgedata.edg.xml"))
        for interval in tree.getroot().iter("interval"):
            for e in list(interval.findall("edge")):
                if e.get("id") in CAPS and e.get("id") not in REAL:
                    interval.remove(e)
        observed = os.path.join(work, "observed_only.edg.xml")
        tree.write(observed)
        fit = detector_fit(observed, os.path.join(work, "edgedata.out.xml"))
        start = start_edges(os.path.join(work, "vehicles.rou.xml"))
        demanded, undeparted = collections.Counter(start.values()), collections.Counter()
        for _, el in ET.iterparse(TRIPINFO):
            if el.tag == "tripinfo":
                if el.get("depart") in (None, "-1", "-1.00"):
                    undeparted[start.get(el.get("id"))] += 1
                el.clear()
        fit.update({"iteration": it, "capped_edges": len(CAPS), "undeparted": sum(undeparted.values()),
                    "vehicles": len(start)})
        print(json.dumps(fit), flush=True)
        os.system(f"rm -rf {evaluate.settings.WORK_DIR}")
        for edge, n in undeparted.items():
            if n / demanded[edge] > THRESHOLD:
                inserted_per_hour = (demanded[edge] - n) / hours
                CAPS[edge] = min(CAPS.get(edge, inserted_per_hour), inserted_per_hour)


if __name__ == "__main__":
    main()
