"""Hold-out validation of the simulated twin: detectors it was NOT calibrated with.

    python tools/calibration/holdout_sim.py <net> <counts.csv> [...]

K-fold at edge level: for each fold the counts of the held-out edges are removed from
routeSampler's input, the full pipeline runs (routeSampler + simulation), and the
simulated vehicles on the held-out edges are compared with their counts. Same naive
baselines as holdout.py (nearest training edge, mean of the same road class).
Env: as evaluate.py (CANDIDATE_ROUTES_FILE, SUMO_ARGS, WARMUP_HOURS), FOLDS (5), SEED (1).
"""
import json
import os
import random
import shutil
import statistics
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(__file__))
import evaluate  # noqa: E402
from holdout import metrics  # noqa: E402
from sumo_controller import SumoController  # noqa: E402

import sumolib  # noqa: E402

FOLDS = int(os.getenv("FOLDS", "5"))
SEED = int(os.getenv("SEED", "1"))
HELD = set()
OBSERVED = {}  # edge -> count of the measured (last) interval, filled on every run

_orig = SumoController._SumoController__generate_edgedata


def _without_held(self):
    _orig(self)
    tree = ET.parse(self.edgedata_file)
    intervals = list(tree.getroot().iter("interval"))
    for i, interval in enumerate(intervals):
        for e in list(interval.findall("edge")):
            if i == len(intervals) - 1:
                OBSERVED[e.get("id")] = float(e.get("passenger"))
            if e.get("id") in HELD:
                interval.remove(e)
    tree.write(self.edgedata_file)


SumoController._SumoController__generate_edgedata = _without_held


def simulated(path):
    res = {}
    for _, el in ET.iterparse(path):
        if el.tag == "interval":
            res = {e.get("id"): float(e.get("entered", 0)) for e in el.findall("edge")}
            el.clear()
    return res


def main():
    net_file, *samples = sys.argv[1:]
    routes = os.environ["CANDIDATE_ROUTES_FILE"]
    os.environ["KEEP_WORK_DIR"] = "1"
    net = sumolib.net.readNet(net_file)

    def mid(e):
        s = net.getEdge(e).getShape()
        return s[len(s) // 2]

    def cls(e):
        return (net.getEdge(e).getType() or "?").split(".")[-1]

    summary = {m: [] for m in ("twin", "twin_train", "nearest", "class_mean")}
    for sample in samples:
        detectors, flows = evaluate.inputs(sample)
        HELD.clear()
        OBSERVED.clear()
        evaluate.run(net_file, routes, detectors, flows)  # once, to know the edges with counts
        shutil.rmtree(evaluate.settings.WORK_DIR, ignore_errors=True)
        obs = dict(OBSERVED)
        edges = sorted(obs)
        random.Random(SEED).shuffle(edges)
        pairs = {m: [] for m in summary}
        for k in range(FOLDS):
            test = edges[k::FOLDS]
            HELD.clear()
            HELD.update(test)
            evaluate.run(net_file, routes, detectors, flows)
            sim = simulated(os.path.join(evaluate.settings.WORK_DIR, "cal", "edgedata.out.xml"))
            shutil.rmtree(evaluate.settings.WORK_DIR, ignore_errors=True)
            train = {e: obs[e] for e in edges if e not in HELD}
            by_class = {}
            for e, n in train.items():
                by_class.setdefault(cls(e), []).append(n)
            tm = [(e, mid(e)) for e in train]
            for e in test:
                pairs["twin"].append((obs[e], sim.get(e, 0.0)))
                m = mid(e)
                near = min(tm, key=lambda t: (t[1][0] - m[0]) ** 2 + (t[1][1] - m[1]) ** 2)[0]
                pairs["nearest"].append((obs[e], train[near]))
                vals = by_class.get(cls(e))
                pairs["class_mean"].append((obs[e], statistics.mean(vals) if vals else statistics.mean(train.values())))
            pairs["twin_train"] += [(train[e], sim.get(e, 0.0)) for e in train]
        res = {m: metrics(p) for m, p in pairs.items()}
        print(os.path.basename(sample), json.dumps(res), flush=True)
        for m in summary:
            summary[m].append(res[m])
    print("MEAN", json.dumps({m: {k: round(statistics.mean(r[k] for r in rs), 3) for k in ("geh_under_5", "mae", "est_over_obs", "r")}
                              for m, rs in summary.items()}))


if __name__ == "__main__":
    main()
