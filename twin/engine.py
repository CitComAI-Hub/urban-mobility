"""Traffic twin of one zone: calibration with the EU LDT Toolbox model, and scenarios.

Calibration (every few minutes, with the counts read through the data space): the
Toolbox's SumoController maps the counts to the zone network, routeSampler picks the
routes that reproduce them and a mesoscopic simulation checks the fit. The sampled
vehicles are the demand of the twin.

Scenarios reuse that demand: vehicles whose route uses a closed street are re-routed by
the fastest path that avoids it (or dropped if they start or end on it), the demand can
be scaled, and the same simulator runs the baseline and the scenario, so the
differences come only from the change.
"""
import heapq
import logging
import math
import os
import random
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import sumolib

from config import settings as toolbox_settings
from sdm_output import detector_fit
from sumo_controller import SumoController

logger = logging.getLogger(__name__)

WARMUP_HOURS = 1  # the measured hour is simulated after one hour with the same demand
HOUR = 3600
SUMO_ARGS = [  # validated on the zone (calibration/zones, 2026-11-19)
    "--meso-junction-control.limited", "true", "--meso-multi-queue", "true", "--meso-lane-queue", "true",
    "--ignore-route-errors", "true", "--no-step-log", "true", "--time-to-teleport", "300",
]
# Drivers re-route every 2 minutes by the current travel times. Without it, every
# vehicle affected by a closure takes the same free-flow detour and the zone locks up;
# with it the calibration fit is the same (85.2 % at 13:00 on 2026-11-19) and a +-20 %
# demand changes delay and CO2 smoothly.
REROUTING = ["--device.rerouting.probability", "1", "--device.rerouting.period", "120",
             "--device.rerouting.pre-period", "0"]


class Zone:
    """The zone network: geometry for the map and a graph for re-routing."""

    def __init__(self, net_file: str, routes_file: str, name: str, bbox: list[float] | None = None):
        """bbox: lon0, lat0, lon1, lat1 of the zone (the counts used); default, the network's."""
        self.net_file, self.routes_file, self.name = net_file, routes_file, name
        self.net = sumolib.net.readNet(net_file)
        self.edges = {}
        for e in self.net.getEdges():
            if e.isSpecial() or not e.allows("passenger"):
                continue
            self.edges[e.getID()] = e
        (x0, y0), (x1, y1) = self.net.getBBoxXY()
        self.bbox = bbox or [*self.net.convertXY2LonLat(x0, y0), *self.net.convertXY2LonLat(x1, y1)]

    def geometry(self) -> list[dict]:
        out = []
        for eid, e in self.edges.items():
            pts = [self.net.convertXY2LonLat(x, y) for x, y in e.getShape()]
            out.append({"id": eid, "name": e.getName() or "", "lanes": e.getLaneNumber(),
                        "c": [[round(lat, 6), round(lon, 6)] for lon, lat in pts]})
        return out

    def inside(self, lon: float, lat: float, margin: float = 0.0004) -> bool:
        lon0, lat0, lon1, lat1 = self.bbox
        return lon0 + margin < lon < lon1 - margin and lat0 + margin < lat < lat1 - margin

    def fastest(self, start: str, end: str, closed: set[str]) -> list[str] | None:
        """Fastest path (free-flow time) from start to end avoiding the closed edges."""
        if start in closed or end in closed:
            return None
        cost = {start: 0.0}
        prev = {}
        heap = [(0.0, start)]
        while heap:
            c, eid = heapq.heappop(heap)
            if eid == end:
                path = [end]
                while path[-1] != start:
                    path.append(prev[path[-1]])
                return path[::-1]
            if c > cost.get(eid, math.inf):
                continue
            for nxt in self.edges[eid].getOutgoing():
                nid = nxt.getID()
                if nid in closed or nid not in self.edges:
                    continue
                nc = c + nxt.getLength() / max(nxt.getSpeed(), 1.0)
                if nc < cost.get(nid, math.inf):
                    cost[nid], prev[nid] = nc, eid
                    heapq.heappush(heap, (nc, nid))
        return None


@dataclass
class Result:
    flows: dict = field(default_factory=dict)  # edge -> vehicles in the measured hour
    co2: dict = field(default_factory=dict)  # edge -> kg CO2 in the measured hour
    metrics: dict = field(default_factory=dict)


@dataclass
class Baseline:
    observed_at: str
    detectors: list  # [{id, lon, lat, edge, observed, simulated, ok}]
    fit: dict
    vehicles: list  # [(id, depart, [edges])]
    result: Result


def _counts_inputs(detectors: list[dict], flows: list[list]):
    """Toolbox flow rows for the warm-up hour and the measured one."""
    rows = [[d, minute, n] for d, _, n in flows for minute in range(0, (WARMUP_HOURS + 1) * 60, 60)]
    return detectors, rows


def _read_vehicles(path: str) -> list:
    named, vehicles = {}, []
    for _, el in ET.iterparse(path):
        if el.tag == "route" and el.get("id"):
            named[el.get("id")] = el.get("edges").split()
        elif el.tag == "vehicle":
            child = el.find("route")
            edges = child.get("edges").split() if child is not None else named.get(el.get("route"), [])
            vehicles.append((el.get("id"), float(el.get("depart")), edges))
            el.clear()
    return vehicles


def _read_vehicles_from_candidates(path: str, limit: int | None = None) -> list:
    """(id, 0, edges) of the candidate routes (tests and tools)."""
    import gzip
    out = []
    with (gzip.open(path) if path.endswith(".gz") else open(path, "rb")) as fh:
        for _, el in ET.iterparse(fh):
            if el.tag == "route" and el.get("edges"):
                out.append((el.get("id") or str(len(out)), 0.0, el.get("edges").split()))
                if limit and len(out) >= limit:
                    break
            elif el.tag == "vehicle":
                el.clear()
    return out


def _write_vehicles(path: str, vehicles: list):
    with open(path, "w") as f:
        f.write("<routes>\n")
        f.writelines(f'  <vehicle id="{vid}" depart="{depart:.2f}"><route edges="{" ".join(edges)}"/></vehicle>\n' for vid, depart, edges in sorted(vehicles, key=lambda v: v[1]))
        f.write("</routes>\n")


def _last_interval(path: str, attr: str) -> dict:
    res = {}
    for _, el in ET.iterparse(path):
        if el.tag == "interval":
            if float(el.get("begin")) >= WARMUP_HOURS * HOUR:
                res = {e.get("id"): float(e.get(attr, 0)) for e in el.findall("edge")}
            el.clear()
    return res


def simulate(zone: Zone, vehicles: list, workdir: str, extra_args: list | None = None) -> Result:
    """Mesoscopic simulation of the vehicles; flows, CO2 and trip metrics of the
    measured hour."""
    os.makedirs(workdir, exist_ok=True)
    routes = os.path.join(workdir, "vehicles.rou.xml")
    _write_vehicles(routes, vehicles)
    add = os.path.join(workdir, "out.add.xml")
    traffic, emis, trips = (os.path.join(workdir, n) for n in ("traffic.xml", "emissions.xml", "tripinfo.xml"))
    with open(add, "w") as f:
        f.write(f'<additional><edgeData id="t" file="{traffic}" period="{HOUR}"/>'
                f'<edgeData id="e" type="emissions" file="{emis}" period="{HOUR}" excludeEmpty="true"/></additional>')
    cmd = ["sumo", "-n", zone.net_file, "-r", routes, "--mesosim", "true", "--end", str((WARMUP_HOURS + 1) * HOUR),
           "-a", add, "--device.emissions.probability", "1", "--tripinfo-output", trips,
           "--tripinfo-output.write-undeparted", "true"] + SUMO_ARGS + REROUTING + list(extra_args or [])
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    res = Result(flows=_last_interval(traffic, "entered"),
                 co2={e: v / 1e6 for e, v in _last_interval(emis, "CO2_abs").items()})
    n = undeparted = 0
    duration = time_loss = length = 0.0
    for _, el in ET.iterparse(trips):
        if el.tag == "tripinfo":
            depart = float(el.get("depart", -1))
            if depart < 0:
                undeparted += 1
            elif depart >= WARMUP_HOURS * HOUR and float(el.get("arrival", -1)) >= 0:
                n += 1
                duration += float(el.get("duration"))
                time_loss += float(el.get("timeLoss"))
                length += float(el.get("routeLength"))
            el.clear()
    res.metrics = {
        "trips": n,
        "mean_trip_minutes": round(duration / n / 60, 2) if n else None,
        "mean_delay_minutes": round(time_loss / n / 60, 2) if n else None,
        "total_delay_hours": round(time_loss / 3600, 1),
        "vehicle_km": round(length / 1000),
        "co2_kg": round(sum(res.co2.values())),
        "not_departed": undeparted,
    }
    return res


def calibrate(zone: Zone, detectors: list[dict], flows: list[list], observed_at: str, workdir: str) -> Baseline:
    """Toolbox pipeline (SumoController: detectors -> routeSampler -> simulation) on the
    zone, then the baseline run of the twin with the sampled vehicles."""
    os.makedirs(workdir, exist_ok=True)
    inside = [d for d in detectors if zone.inside(d["lon"], d["lat"])]
    ids = {int(d["detector_id"]) for d in inside}
    detectors, rows = _counts_inputs(inside, [f for f in flows if f[0] in ids])
    toolbox_settings.WORK_DIR = tempfile.mkdtemp(prefix="twin-cal-", dir=workdir)
    controller = SumoController(
        name="zone", network_url=None, network_file=zone.net_file, detectors=detectors, flow_data=rows,
        flow_interval_minutes=60, simulation_datetime="2026-01-01T00:00:00", meso_simulation=True,
        upload_results=False, edge_emissions=True, cleanup=False, sumo_args=SUMO_ARGS,
        route_sampler_args=["--optimize", "full"], candidate_routes_file=zone.routes_file)
    try:
        if controller.run() is None and not os.path.exists(controller.vehicles_file):
            raise RuntimeError("the Toolbox pipeline failed")
        fit = detector_fit(controller.edgedata_file, controller.edgedata_output)
        observed = _last_interval(controller.edgedata_file, "passenger")
        det_edge = {}
        for _, el in ET.iterparse(controller.detectors_file):
            if el.tag == "e1Detector":
                det_edge[el.get("id")] = el.get("lane").rsplit("_", 1)[0]
        vehicles = _read_vehicles(controller.vehicles_file)
    finally:
        shutil.rmtree(toolbox_settings.WORK_DIR, ignore_errors=True)
    result = simulate(zone, vehicles, os.path.join(workdir, "baseline"))
    points = []
    for d in inside:
        edge = det_edge.get(str(d["detector_id"]))
        if edge is None or edge not in observed:
            continue
        o, s = observed[edge], result.flows.get(edge, 0.0)
        geh = math.sqrt(2 * (s - o) ** 2 / (s + o)) if s + o else 0.0
        points.append({"id": d["detector_id"], "lon": d["lon"], "lat": d["lat"], "edge": edge,
                       "observed": round(o), "simulated": round(s), "ok": geh < 5})
    fit["detectors_ok"] = round(sum(p["ok"] for p in points) / len(points), 3) if points else None
    return Baseline(observed_at=observed_at, detectors=points, fit=fit, vehicles=vehicles, result=result)


def scenario(zone: Zone, baseline: Baseline, closed: set[str], demand: float, workdir: str, seed: int = 1):
    """The baseline demand with closed streets and a demand factor."""
    rng = random.Random(seed)
    vehicles, rerouted, dropped = [], 0, 0
    cache = {}
    for vid, depart, edges in baseline.vehicles:
        copies = [vid] if demand >= 1 or rng.random() < demand else []
        if demand > 1 and rng.random() < demand - 1:
            copies.append(vid + ".x")
        if not copies:
            continue
        route = edges
        if closed and closed.intersection(edges):
            key = (edges[0], edges[-1])
            if key not in cache:
                cache[key] = zone.fastest(edges[0], edges[-1], closed)
            route = cache[key]
            if route is None:
                dropped += len(copies)
                continue
            rerouted += len(copies)
        for i, c in enumerate(copies):
            vehicles.append((c, depart + (i * 1.7), route))
    result = simulate(zone, vehicles, workdir)
    result.metrics.update({"rerouted": rerouted, "trips_impossible": dropped})
    return result
