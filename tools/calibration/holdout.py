"""Can routeSampler estimate the traffic on streets WITHOUT a measurement point?

    python tools/calibration/holdout.py <net> <counts.csv> [...]

K-fold cross-validation at edge level: the edges with counts are split into K folds;
for each fold, routeSampler is fitted to the other folds only, and the vehicles of the
chosen routes on the held-out edges are compared with their counts. No simulation:
this is the estimate the service would give (demand reconstruction only).

Two naive baselines on the same held-out edges, so that the numbers mean something:
- nearest: the count of the nearest training edge (edge midpoints, metres).
- class_mean: the mean count of the training edges of the same road class (OSM highway).

Env: CANDIDATE_ROUTES_FILE, FOLDS (5), SEED (1), SUMO_THREADS.
"""

import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("WARMUP_HOURS", "0")  # one interval: no simulation here
import evaluate  # noqa: E402
from config import settings  # noqa: E402
from sumo_controller import SumoController  # noqa: E402

import sumolib  # noqa: E402

FOLDS = int(os.getenv("FOLDS", "5"))
SEED = int(os.getenv("SEED", "1"))


def observed_edges(net_file, routes, detectors, flows) -> dict:
    """edge -> vehicles/h, through the same steps as the job (detectors on lanes,
    edgeDataFromFlow)."""
    settings.WORK_DIR = tempfile.mkdtemp(prefix="sumo-ho-")
    c = SumoController(name="ho", network_url=None, network_file=net_file, detectors=detectors,
                       flow_data=flows, flow_interval_minutes=60, simulation_datetime="2026-01-01T00:00:00",
                       meso_simulation=True, upload_results=False, cleanup=False,
                       candidate_routes_file=routes)
    c._SumoController__create_domain()
    c._SumoController__process_detectors()
    c._SumoController__process_flow_data()
    c._SumoController__generate_edgedata()
    obs = {}
    for _, el in ET.iterparse(c.edgedata_file):
        if el.tag == "edge" and el.get("passenger") is not None:
            obs[el.get("id")] = float(el.get("passenger"))
    return obs


def route_sampler(routes, train: dict, workdir: str) -> dict:
    """Vehicles per edge in the routes routeSampler picks to match `train`."""
    edg = os.path.join(workdir, "train.edg.xml")
    with open(edg, "w") as f:
        f.write('<data><interval id="h" begin="0" end="3600">')
        for e, n in train.items():
            f.write(f'<edge id="{e}" passenger="{n:g}"/>')
        f.write("</interval></data>")
    out = os.path.join(workdir, "sampled.rou.xml")
    cmd = [sys.executable, os.path.join(settings.SUMO_DIR, "tools", "routeSampler.py"), "-r", routes,
           "--edgedata-files", edg, "--edgedata-attribute", "passenger", "-o", out,
           "--threads", str(settings.AVAILABLE_PROCESSORS), "--optimize", "full"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    counts, named = {}, {}
    for _, el in ET.iterparse(out):
        if el.tag == "route" and el.get("id"):
            named[el.get("id")] = el.get("edges", "").split()  # shared route definitions
        elif el.tag == "vehicle":
            child = el.find("route")
            edges = child.get("edges", "").split() if child is not None else named.get(el.get("route"), [])
            for e in edges:
                counts[e] = counts.get(e, 0) + 1
            el.clear()
    return counts


def metrics(pairs) -> dict:
    """pairs: (observed, estimated)."""
    geh = [math.sqrt(2 * (s - o) ** 2 / (s + o)) if s + o > 0 else 0.0 for o, s in pairs]
    obs = [o for o, _ in pairs]
    est = [s for _, s in pairs]
    r = statistics.correlation(obs, est) if len(set(est)) > 1 else float("nan")
    return {"n": len(pairs), "geh_under_5": round(sum(g < 5 for g in geh) / len(geh), 3),
            "mae": round(statistics.mean(abs(s - o) for o, s in pairs), 1),
            "est_over_obs": round(sum(est) / sum(obs), 3) if sum(obs) else None,
            "r": round(r, 3)}


def main():
    net_file, *samples = sys.argv[1:]
    routes = os.getenv("CANDIDATE_ROUTES_FILE")
    net = sumolib.net.readNet(net_file)

    def mid(e):
        shape = net.getEdge(e).getShape()
        return shape[len(shape) // 2]

    def cls(e):
        return (net.getEdge(e).getType() or "?").split(".")[-1]

    summary = {"routeSampler": [], "nearest": [], "class_mean": [], "routeSampler_train": []}
    for sample in samples:
        t0 = time.time()
        detectors, flows = evaluate.inputs(sample)
        obs = observed_edges(net_file, routes, detectors, flows)
        edges = sorted(obs)
        random.Random(SEED).shuffle(edges)
        folds = [edges[i::FOLDS] for i in range(FOLDS)]
        held = {"routeSampler": [], "nearest": [], "class_mean": [], "routeSampler_train": []}
        work = tempfile.mkdtemp(prefix="sumo-ho-rs-")
        for k, test in enumerate(folds):
            test_set = set(test)
            train = {e: obs[e] for e in edges if e not in test_set}
            est = route_sampler(routes, train, work)
            by_class = {}
            for e, n in train.items():
                by_class.setdefault(cls(e), []).append(n)
            all_mean = statistics.mean(train.values())
            train_mid = [(e, mid(e)) for e in train]
            for e in test:
                held["routeSampler"].append((obs[e], est.get(e, 0)))
                m = mid(e)
                near = min(train_mid, key=lambda t: (t[1][0] - m[0]) ** 2 + (t[1][1] - m[1]) ** 2)[0]
                held["nearest"].append((obs[e], train[near]))
                vals = by_class.get(cls(e))
                held["class_mean"].append((obs[e], statistics.mean(vals) if vals else all_mean))
            held["routeSampler_train"] += [(train[e], est.get(e, 0)) for e in train]
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(settings.WORK_DIR, ignore_errors=True)
        res = {m: metrics(p) for m, p in held.items()}
        print(os.path.basename(sample), round(time.time() - t0), "s", json.dumps(res), flush=True)
        for m in summary:
            summary[m].append(res[m])
    print("MEAN", json.dumps({m: {k: round(statistics.mean(r[k] for r in rs), 3) for k in ("geh_under_5", "mae", "est_over_obs", "r")}
                              for m, rs in summary.items()}))


if __name__ == "__main__":
    main()
