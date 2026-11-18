"""Fit of the simulation to the counts, for calibrating the network.

    python tools/calibration/evaluate.py <net> <counts.csv> [...]

counts.csv: samples of the traffic counts read through the data space
(TrafficFlowObserved of the offering traffic-flow): idpm,ih,heading,lon,lat,dateObserved.
For each sample, runs the same pipeline as the data space job (SumoController with
the job's default options) and prints the fit; then the mean over the samples.
Extra SUMO options: SUMO_ARGS (JSON list), as in the job.
"""

import csv
import json
import os
import shutil
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import dataspace_job  # noqa: E402
from config import settings  # noqa: E402
from sdm_output import detector_fit  # noqa: E402
from sumo_controller import SumoController  # noqa: E402


# Hours simulated before the measured one, with the same demand, so that vehicles
# that depart late in the hour are already on the network (WARMUP_HOURS env var).
WARMUP_HOURS = int(os.getenv("WARMUP_HOURS", "1"))


def inputs(counts_file: str):
    """A sample of the counts read through the data space (collect_ds.py): idpm, ih
    (veh/h), heading, lon, lat, dateObserved."""
    detectors, flows = [], []
    with open(counts_file) as fh:
        for row in csv.DictReader(fh):
            if not row["ih"] or float(row["ih"]) < 0 or not row["heading"]:
                continue
            detectors.append({"detector_id": row["idpm"], "lon": float(row["lon"]), "lat": float(row["lat"]),
                              "heading": float(row["heading"])})
            for minute in range(0, 60 * (WARMUP_HOURS + 1), 60):
                flows.append([int(row["idpm"]), minute, int(float(row["ih"]))])
    return detectors, flows


def run(net: str, routes: str, detectors, flows) -> dict:
    settings.WORK_DIR = tempfile.mkdtemp(prefix="sumo-cal-")
    controller = SumoController(
        name="cal",
        network_url=None,
        network_file=net,
        detectors=detectors,
        flow_data=flows,
        flow_interval_minutes=60,
        simulation_datetime="2026-01-01T00:00:00",
        meso_simulation=True,
        upload_results=False,
        edge_emissions=True,  # cheap; the per-vehicle output is not needed
        cleanup=False,
        sumo_args=json.loads(os.getenv("SUMO_ARGS", "null")) or dataspace_job.DEFAULT_SUMO_ARGS,
        route_sampler_args=dataspace_job.DEFAULT_ROUTE_SAMPLER_ARGS,
        candidate_routes_file=routes,
    )
    try:
        if controller.run() is None:
            return {}
        fit = detector_fit(controller.edgedata_file, controller.edgedata_output)
        sampled = detector_fit(controller.edgedata_file, _sampled_edgedata(controller))
        fit["route_sampler_geh_under_5"] = sampled.get("geh_under_5")
        return fit
    finally:
        if not os.getenv("KEEP_WORK_DIR"):
            shutil.rmtree(settings.WORK_DIR, ignore_errors=True)
        else:
            print("work dir", settings.WORK_DIR)


def _sampled_edgedata(controller) -> str:
    """Vehicles per edge in the routes routeSampler chose for the measured hour (what
    the simulation would give with unlimited capacity), as an edgeData file."""
    import xml.etree.ElementTree as ET
    start = WARMUP_HOURS * 3600
    counts = {}
    for _, el in ET.iterparse(controller.vehicles_file):
        if el.tag == "vehicle":
            route = el.find("route")
            if route is not None and float(el.get("depart", 0)) >= start:
                for e in route.get("edges", "").split():
                    counts[e] = counts.get(e, 0) + 1
            el.clear()
    path = controller.vehicles_file + ".edg.xml"
    with open(path, "w") as f:
        f.write('<meandata><interval begin="0" end="3600">')
        for e, n in counts.items():
            f.write(f'<edge id="{e}" entered="{n}"/>')
        f.write("</interval></meandata>")
    return path


def main():
    net, *samples = sys.argv[1:]
    routes = os.getenv("CANDIDATE_ROUTES_FILE", os.path.join(os.path.dirname(net), "candidate.rou.xml.gz"))
    results = []
    for sample in samples:
        detectors, flows = inputs(sample)
        t0 = time.time()
        fit = run(net, routes, detectors, flows)
        fit["seconds"] = round(time.time() - t0)
        print(os.path.basename(sample), json.dumps(fit), flush=True)
        if fit.get("geh_under_5") is not None:
            results.append(fit)
    if results:
        print("MEAN", json.dumps({k: round(statistics.mean(r[k] for r in results), 3)
                                  for k in ("geh_under_5", "simulated_over_observed", "route_sampler_geh_under_5")}))


if __name__ == "__main__":
    main()
