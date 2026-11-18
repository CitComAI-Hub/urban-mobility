"""SUMO results as Smart Data Models entities (normalized NGSI-LD), one per edge:

- TrafficFlowObserved (dataModel.Transportation): simulated vehicles per hour, mean
  speed and occupancy of the edge in the simulated interval.
- CarbonFootprint (dataModel.Environment): CO2 emitted on the edge in that interval,
  in tonnes (SUMO's default emission model, HBEFA; CO2 only, tank-to-wheel).

Only edges with traffic in the interval are published. The entities say in
`description` and `dataProvider` that they are simulated. Other pollutants (NOx, PM)
have no fitting Smart Data Model (AirQualityObserved and TrafficEnvironmentImpact
describe concentrations, not emitted mass), so they are not published.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

TRANSPORTATION_CONTEXT = (
    "https://raw.githubusercontent.com/smart-data-models/"
    "dataModel.Transportation/refs/heads/master/context.jsonld"
)
ENVIRONMENT_CONTEXT = (
    "https://raw.githubusercontent.com/smart-data-models/"
    "dataModel.Environment/refs/heads/master/context.jsonld"
)

SOURCE = "https://github.com/CitComAI-Hub/urban-mobility"


def entity_key(edge_id: str) -> str:
    """SUMO edge id -> part of an NGSI-LD id: '#' (an URI fragment) becomes '~'
    (unreserved), anything else outside the URI unreserved set is percent-encoded."""
    return quote(edge_id.replace("#", "~"), safe="-._~")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _intervals(path: str):
    """(begin_seconds, end_seconds, {edge_id: attributes}) of an edgeData output."""
    for interval in ET.parse(path).getroot().iter("interval"):
        edges = {e.attrib["id"]: e.attrib for e in interval.iter("edge")}
        yield float(interval.attrib["begin"]), float(interval.attrib["end"]), edges


def _geometry(network, edge_id: str):
    edge = network.getEdge(edge_id)
    coords = [list(network.convertXY2LonLat(x, y)) for x, y in edge.getShape()]
    return edge, {"type": "LineString", "coordinates": [[round(c, 7) for c in p] for p in coords]}


def _number(attrs: dict, key: str) -> float | None:
    try:
        value = float(attrs[key])
    except (KeyError, ValueError):
        return None
    return value if math.isfinite(value) else None


def build_entities(
    network,
    edgedata_file: str,
    emissions_file: str | None,
    start: datetime,
    domain: str,
    data_provider: str,
    description: str,
) -> tuple[list[dict], list[dict]]:
    """(TrafficFlowObserved entities, CarbonFootprint entities) of the last interval."""
    flows: list[dict] = []
    footprints: list[dict] = []

    last = None
    for last in _intervals(edgedata_file):
        pass
    if last is None:
        return flows, footprints
    begin, end, edges = last
    emissions = {}
    if emissions_file:
        for b, e, em in _intervals(emissions_file):
            if b == begin and e == end:
                emissions = em
    observed_from = _iso(start + timedelta(seconds=begin))
    observed_to = _iso(start + timedelta(seconds=end))
    per_hour = 3600.0 / (end - begin)

    for edge_id, attrs in edges.items():
        if edge_id.startswith(":"):  # internal edges of junctions
            continue
        entered = _number(attrs, "entered")
        if not entered:
            continue
        edge, geometry = _geometry(network, edge_id)
        key = entity_key(edge_id)
        flow_id = f"urn:ngsi-ld:TrafficFlowObserved:{domain}:sim:{key}"
        flow = {
            "id": flow_id,
            "type": "TrafficFlowObserved",
            "location": {"type": "GeoProperty", "value": geometry},
            "dateObserved": {"type": "Property", "value": observed_to},
            "dateObservedFrom": {"type": "Property", "value": observed_from},
            "dateObservedTo": {"type": "Property", "value": observed_to},
            "intensity": {"type": "Property", "value": round(entered * per_hour)},
            "dataProvider": {"type": "Property", "value": data_provider},
            "source": {"type": "Property", "value": SOURCE},
            "description": {"type": "Property", "value": description},
        }
        if edge.getName():
            flow["name"] = {"type": "Property", "value": edge.getName()}
        speed = _number(attrs, "speed")
        if speed is not None:
            flow["averageVehicleSpeed"] = {"type": "Property", "value": round(speed * 3.6, 1)}
        occupancy = _number(attrs, "occupancy")
        if occupancy is not None:
            # SUMO gives a percentage; the model a fraction of the observation time
            flow["occupancy"] = {"type": "Property", "value": round(min(occupancy, 100.0) / 100.0, 4)}
        flows.append(flow)

        co2 = _number(emissions.get(edge_id, {}), "CO2_abs")  # mg in the interval
        if co2:
            footprints.append({
                "id": f"urn:ngsi-ld:CarbonFootprint:{domain}:sim:{key}",
                "type": "CarbonFootprint",
                "location": {"type": "GeoProperty", "value": geometry},
                "emissionSource": {"type": "Property", "value": "Transport"},
                "CO2eq": {"type": "Property", "value": round(co2 / 1e9, 9)},
                "emissionDate": {"type": "Property", "value": observed_to},
                "relatedSource": {"type": "Relationship", "object": flow_id},
                "tags": {"type": "Property", "value": ["road traffic", "simulation"]},
                "dataProvider": {"type": "Property", "value": data_provider},
                "source": {"type": "Property", "value": SOURCE},
                "description": {
                    "type": "Property",
                    "value": description + " CO2 emitted by road traffic on this street between "
                    f"{observed_from} and {observed_to} (SUMO emission model, CO2 only).",
                },
            })
    return flows, footprints


def detector_fit(observed_edgedata_file: str, simulated_edgedata_file: str) -> dict:
    """How well the simulation reproduces the counts, per edge with detectors: the
    observed vehicles (edgeDataFromFlow output, what routeSampler aims at) against
    the simulated ones that entered the edge, in the LAST interval (the earlier ones
    are warm-up). GEH statistic and totals."""
    def last(path, attr):
        result = None
        for _, _, edges in _intervals(path):
            result = {e: (_number(a, attr) or 0.0) for e, a in edges.items()}
        return result or {}

    observed = last(observed_edgedata_file, "passenger")
    simulated = last(simulated_edgedata_file, "entered")
    if not observed:
        return {}
    geh, pairs = [], []
    for edge_id, obs in observed.items():
        sim = simulated.get(edge_id, 0.0)
        geh.append(math.sqrt(2 * (sim - obs) ** 2 / (sim + obs)) if sim + obs > 0 else 0.0)
        pairs.append((obs, sim))
    obs_total = sum(o for o, _ in pairs)
    return {
        "edges": len(pairs),
        "geh_under_5": round(sum(g < 5 for g in geh) / len(geh), 3),
        "simulated_over_observed": round(sum(s for _, s in pairs) / obs_total, 3) if obs_total else None,
    }
