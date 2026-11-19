"""Web service of the traffic twin (Flask): current state of the zone and scenarios.

    gunicorn --workers 1 --threads 8 'server:create_app()'   (PYTHONPATH=src:twin)

Environment:
  ZONE_NET, ZONE_ROUTES, ZONE_NAME   zone network, candidate routes, name shown
  ZONE_BBOX                          lon0,lat0,lon1,lat1 of the counts used (default: the network's)
  TRAFFIC_SOURCE_URL                 provider gateway of the counts (…/ngsi-ld/v1 is added)
  DS_ISSUER_URL ...                  ds-consumer-auth settings (read through the data space)
  COUNTS_CSV                         instead of the broker: a CSV of counts (development)
  REFRESH_MINUTES (15), MAX_AGE_MINUTES (90), WORK_DIR, MAX_QUEUED (5)
"""
import csv
import logging
import os
import queue
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_from_directory

import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("twin")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def read_counts():
    """(detectors, flows, observed_at) from the data space, or from COUNTS_CSV."""
    if os.getenv("COUNTS_CSV"):
        detectors, flows, times = [], [], []
        with open(os.environ["COUNTS_CSV"]) as fh:
            for r in csv.DictReader(fh):
                if r["ih"] and float(r["ih"]) >= 0 and r["heading"]:
                    detectors.append({"detector_id": r["idpm"], "lon": float(r["lon"]), "lat": float(r["lat"]),
                                      "heading": float(r["heading"])})
                    flows.append([int(r["idpm"]), 0, int(float(r["ih"]))])
                    times.append(r["dateObserved"])
        return detectors, flows, max(times) if times else ""
    import requests
    from dataspace_job import read_counts as read_entities, to_inputs
    session = requests.Session()
    if os.getenv("DS_ISSUER_URL"):
        from ds_consumer_auth import data_space_session
        session = data_space_session()
    entities = [e for e in read_entities(session, os.environ["TRAFFIC_SOURCE_URL"], "TrafficFlowObserved")
                if ":pm_" in e["id"]]
    now = datetime.now(timezone.utc)
    detectors, flows, observed = to_inputs(entities, now, timedelta(minutes=int(os.getenv("MAX_AGE_MINUTES", "90"))))
    return detectors, flows, observed.isoformat()


class Twin:
    def __init__(self):
        bbox = [float(x) for x in os.environ["ZONE_BBOX"].split(",")] if os.getenv("ZONE_BBOX") else None
        self.zone = engine.Zone(os.environ["ZONE_NET"], os.environ["ZONE_ROUTES"], os.getenv("ZONE_NAME", "Zone"), bbox)
        self.geometry = self.zone.geometry()
        self.work = os.getenv("WORK_DIR") or tempfile.mkdtemp(prefix="twin-")
        self.baseline = None
        self.error = None
        self.updated = None
        self.jobs = {}
        self.queue = queue.Queue(maxsize=int(os.getenv("MAX_QUEUED", "5")))
        self.lock = threading.Lock()  # one simulation at a time (CPU and memory)

    # current state ---------------------------------------------------------
    def refresh(self):
        try:
            detectors, flows, observed_at = read_counts()
            with self.lock:
                base = engine.calibrate(self.zone, detectors, flows, observed_at, os.path.join(self.work, "cal"))
            self.baseline, self.error, self.updated = base, None, time.time()
            logger.info("Calibrated with counts of %s: %s of the detectors within GEH 5", observed_at,
                        base.fit.get("detectors_ok"))
        except Exception as exc:  # keep the last good state
            logger.exception("Calibration failed")
            self.error = str(exc)

    def refresher(self):
        minutes = int(os.getenv("REFRESH_MINUTES", "15"))
        while True:
            self.refresh()
            time.sleep(minutes * 60)

    # scenarios -------------------------------------------------------------
    def worker(self):
        while True:
            job = self.queue.get()
            job["state"], job["started"] = "running", time.time()
            try:
                base = self.baseline
                with self.lock:
                    res = engine.scenario(self.zone, base, set(job["closed"]), job["demand"],
                                          os.path.join(self.work, "scn", job["id"]))
                job["result"] = {
                    "observed_at": base.observed_at,
                    "baseline": base.result.metrics,
                    "scenario": res.metrics,
                    "delta": {e: round(res.flows.get(e, 0) - base.result.flows.get(e, 0))
                              for e in set(res.flows) | set(base.result.flows)
                              if abs(res.flows.get(e, 0) - base.result.flows.get(e, 0)) >= 10},
                }
                job["state"] = "done"
            except Exception as exc:
                logger.exception("Scenario %s failed", job["id"])
                job["state"], job["error"] = "failed", str(exc)
            job["finished"] = time.time()
            for old in sorted(self.jobs.values(), key=lambda j: j["created"])[:-20]:
                self.jobs.pop(old["id"], None)


def create_app(start: bool = True) -> Flask:
    twin = Twin()
    app = Flask(__name__, static_folder=None)
    if start:
        threading.Thread(target=twin.refresher, daemon=True).start()
        threading.Thread(target=twin.worker, daemon=True).start()

    @app.get("/")
    def home():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/zone")
    def zone():
        return jsonify({"name": twin.zone.name, "bbox": twin.zone.bbox, "edges": twin.geometry})

    @app.get("/api/state")
    def state():
        b = twin.baseline
        if b is None:
            return jsonify({"ready": False, "error": twin.error}), 503
        return jsonify({
            "ready": True, "observed_at": b.observed_at, "updated": twin.updated, "error": twin.error,
            "fit": b.fit, "metrics": b.result.metrics, "detectors": b.detectors,
            "flows": {e: round(v) for e, v in b.result.flows.items() if v >= 1},
            "co2": {e: round(v, 2) for e, v in b.result.co2.items() if v > 0},
        })

    @app.post("/api/scenarios")
    def create_scenario():
        if twin.baseline is None:
            return {"error": "The twin is still calibrating. Try again in a minute."}, 503
        data = request.get_json(silent=True) or {}
        closed = [e for e in data.get("closed", []) if e in twin.zone.edges][:50]
        try:
            demand = float(data.get("demand", 1.0))
        except (TypeError, ValueError):
            return {"error": "demand must be a number"}, 400
        if not 0.5 <= demand <= 1.5:
            return {"error": "demand must be between 0.5 and 1.5"}, 400
        if not closed and demand == 1.0:
            return {"error": "Close a street or change the demand to build a scenario."}, 400
        job = {"id": uuid.uuid4().hex[:12], "state": "queued", "closed": closed, "demand": demand,
               "created": time.time(), "started": None, "finished": None, "result": None, "error": None}
        try:
            twin.queue.put_nowait(job)
        except queue.Full:
            return {"error": "Too many scenarios waiting. Try again in a few minutes."}, 429
        twin.jobs[job["id"]] = job
        return jsonify(job), 202

    @app.get("/api/scenarios/<job_id>")
    def get_scenario(job_id):
        job = twin.jobs.get(job_id)
        if job is None:
            return {"error": "Unknown scenario"}, 404
        return jsonify(job)

    return app
