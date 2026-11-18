"""One simulation of the last hour, with data read through a FIWARE data space.

1. Reads the traffic counts (Smart Data Models TrafficFlowObserved with location,
   heading and intensity, e.g. the measurement points of the Ajuntament de València)
   from the provider's connector, with a token of the data space (ds-consumer-auth:
   the consumer must have acquired the provider's offering).
2. Runs the toolbox's SumoController on a local network: detectors placed by
   direction of travel, routeSampler calibrated to the counts, mesoscopic SUMO with
   emissions per edge.
3. Publishes the result as Smart Data Models (sdm_output) in the consumer's own
   NGSI-LD broker: simulated TrafficFlowObserved and CarbonFootprint per street.

Meant to run as a Kubernetes CronJob: one process, exits 0 on success. Configuration
by environment variables (see README, "CitCom data space job").
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

import requests
from sumolib.net import readNet

from config import settings
from sdm_output import (
    ENVIRONMENT_CONTEXT,
    TRANSPORTATION_CONTEXT,
    build_entities,
    detector_fit,
)
from sumo_controller import SumoController

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dataspace_job")


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        sys.exit(f"Missing environment variable {name}")
    return value


# Meso tuning that brought the uncalibrated OpenStreetMap network of Valencia from
# 38% to 65% of the observed flow at the detectors (2026-11-18). Without it, junction
# and traffic light delays block the network.
DEFAULT_SUMO_ARGS = [
    "--meso-tls-penalty", "0.5",
    "--meso-junction-control.limited", "true",
    "--meso-multi-queue", "true",
    "--meso-lane-queue", "true",
    "--time-to-teleport", "60",
    "--ignore-route-errors", "true",
    "--no-step-log", "true",
]
# Many candidate routes (~70k for Valencia) so that routeSampler can match the counts
# (92% of the counted vehicles, GEH < 5 on 82% of the detector edges).
DEFAULT_RANDOM_TRIPS_ARGS = ["--fringe-factor", "5", "-p", "0.05", "--min-distance", "500", "--seed", "42"]
DEFAULT_ROUTE_SAMPLER_ARGS = ["--optimize", "full"]


def link(context: str) -> str:
    return f'<{context}>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'


def read_counts(session: requests.Session, base_url: str, entity_type: str) -> list[dict]:
    """Every entity of the type (keyValues), page by page."""
    url = f"{base_url.rstrip('/')}/ngsi-ld/v1/entities"
    headers = {"Link": link(TRANSPORTATION_CONTEXT), "Accept": "application/json"}
    entities, offset, limit = [], 0, 1000
    while True:
        response = session.get(
            url,
            params={"type": entity_type, "options": "keyValues", "limit": limit, "offset": offset},
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        page = response.json()
        entities += page
        if len(page) < limit:
            return entities
        offset += limit


def parse_time(value) -> datetime | None:
    if isinstance(value, dict):  # {"@type": "DateTime", "@value": ...}
        value = value.get("@value")
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def to_inputs(entities: list[dict], now: datetime, max_age: timedelta):
    """Counts with position, direction and a recent hourly intensity ->
    (detectors, flow rows, observation time)."""
    detectors, flows, times = [], [], []
    skipped = {"no_location": 0, "no_heading": 0, "no_intensity": 0, "old": 0, "bad_id": 0}
    for e in entities:
        location = e.get("location") or {}
        if location.get("type") != "Point":
            skipped["no_location"] += 1
            continue
        if e.get("heading") is None:
            skipped["no_heading"] += 1
            continue
        intensity = e.get("intensity")
        if not isinstance(intensity, (int, float)) or intensity < 0:
            skipped["no_intensity"] += 1
            continue
        observed = parse_time(e.get("dateObserved"))
        if observed is None or now - observed > max_age:
            skipped["old"] += 1
            continue
        # SumoController's flow file needs integer detector ids: the entity id ends in
        # the number of the measurement point (urn:...:pm_<n>)
        suffix = e["id"].rsplit(":", 1)[-1].split("_")[-1]
        if not suffix.isdigit():
            skipped["bad_id"] += 1
            continue
        lon, lat = location["coordinates"][:2]
        detectors.append({"detector_id": suffix, "lon": lon, "lat": lat, "heading": float(e["heading"])})
        # hourly intensity = vehicles in one 60-minute interval starting at minute 0
        flows.append([int(suffix), 0, int(round(intensity))])
        times.append(observed)
    logger.info("Counts: %s usable, skipped %s", len(detectors), skipped)
    observed_at = max(times) if times else now
    return detectors, flows, observed_at


def upsert(broker: str, entities: list[dict], context: str, batch_size: int = 200) -> int:
    url = f"{broker.rstrip('/')}/ngsi-ld/v1/entityOperations/upsert"
    headers = {"Content-Type": "application/json", "Link": link(context)}
    failed = 0
    for start in range(0, len(entities), batch_size):
        batch = entities[start : start + batch_size]
        body = json.dumps(batch, ensure_ascii=False).encode("utf-8")
        response = requests.post(url, headers=headers, data=body, timeout=120)
        if response.status_code not in (200, 201, 204):
            failed += 1
            logger.error("Upsert %s: %s %s", start, response.status_code, response.text[:1000])
    return failed


def main() -> int:
    source_url = env("TRAFFIC_SOURCE_URL")  # the provider's gateway, e.g. https://apisix-<provider>...
    output_broker = env("OUTPUT_BROKER_URL")  # the consumer's own NGSI-LD broker
    network_file = env("NETWORK_FILE")
    domain = env("SIM_DOMAIN", "valencia")
    entity_type = env("TRAFFIC_ENTITY_TYPE", "TrafficFlowObserved")
    max_age = timedelta(minutes=int(env("MAX_AGE_MINUTES", "90")))
    min_detectors = int(env("MIN_DETECTORS", "100"))
    data_provider = env("DATA_PROVIDER", "VRAIN (Universitat Politècnica de València)")
    description = env(
        "RESULT_DESCRIPTION",
        "Simulated with SUMO (EU LDT Toolbox Urban Mobility) from the traffic counts of the "
        "Ajuntament de València read through the CitCom data space; not a measurement.",
    )

    # Token of the data space for the provider's connector (DS_* variables)
    if os.getenv("DS_ISSUER_URL"):
        from ds_consumer_auth import data_space_session

        session = data_space_session()
    else:  # a broker without access control (local tests)
        session = requests.Session()

    now = datetime.now(timezone.utc)
    detectors, flows, observed_at = to_inputs(read_counts(session, source_url, entity_type), now, max_age)
    if len(detectors) < min_detectors:
        logger.error("Only %s usable counts (minimum %s): no simulation", len(detectors), min_detectors)
        return 1

    # The hourly intensities describe the hour before the observation
    start = (observed_at - timedelta(hours=1)).replace(microsecond=0)
    controller = SumoController(
        name=domain,
        network_url=None,
        network_file=network_file,
        detectors=detectors,
        flow_data=flows,
        flow_interval_minutes=60,
        simulation_datetime=start.isoformat(),
        meso_simulation=True,
        upload_results=False,
        edge_emissions=True,
        cleanup=False,
        sumo_args=json.loads(os.getenv("SUMO_ARGS", "null")) or DEFAULT_SUMO_ARGS,
        random_trips_args=json.loads(os.getenv("RANDOM_TRIPS_ARGS", "null")) or DEFAULT_RANDOM_TRIPS_ARGS,
        route_sampler_args=json.loads(os.getenv("ROUTE_SAMPLER_ARGS", "null")) or DEFAULT_ROUTE_SAMPLER_ARGS,
        candidate_routes_file=os.getenv("CANDIDATE_ROUTES_FILE") or None,
    )
    try:
        result = controller.run()
        if result is None or not os.path.exists(controller.edgedata_output):
            logger.error("Simulation failed")
            return 1
        fit = detector_fit(controller.edgedata_file, controller.edgedata_output)
        logger.info("Fit to the counts: %s", fit)
        network = readNet(network_file)
        flows_out, footprints = build_entities(
            network,
            controller.edgedata_output,
            controller.edge_emissions_output,
            start,
            domain,
            data_provider,
            description,
        )
    finally:
        shutil.rmtree(controller.domain_dir, ignore_errors=True)

    logger.info("Publishing %s TrafficFlowObserved and %s CarbonFootprint", len(flows_out), len(footprints))
    failed = upsert(output_broker, flows_out, TRANSPORTATION_CONTEXT)
    failed += upsert(output_broker, footprints, ENVIRONMENT_CONTEXT)
    if failed:
        logger.error("%s batch(es) failed", failed)
        return 1
    logger.info("Done: simulated %s to %s", start.isoformat(), (start + timedelta(hours=1)).isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
