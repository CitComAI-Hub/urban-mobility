"""Tests of the CitCom data space job: inputs from NGSI-LD, detector placement by
direction of travel, and the Smart Data Models output. They build a small grid
network with netgenerate (eclipse-sumo)."""

import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sumolib.net import readNet  # noqa: E402

import dataspace_job  # noqa: E402
import sdm_output  # noqa: E402
from sumo_controller import _angle_diff, _lane_bearing  # noqa: E402

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def grid(tmp_path_factory):
    """3x3 grid of two-way streets (~170 m blocks) at Valencia's coordinates,
    georeferenced (netconvert from nodes in lon/lat)."""
    netconvert = shutil.which("netconvert")
    if netconvert is None:
        pytest.skip("netconvert not installed")
    d = tmp_path_factory.mktemp("net")
    nodes = [(i, j) for i in range(3) for j in range(3)]
    (d / "n.nod.xml").write_text("<nodes>" + "".join(
        f'<node id="n{i}{j}" x="{-0.38 + 0.002 * i}" y="{39.47 + 0.0015 * j}"/>' for i, j in nodes
    ) + "</nodes>")
    edges = []
    for i, j in nodes:
        for di, dj in ((1, 0), (0, 1)):
            if (i + di, j + dj) in nodes:
                a, b = f"n{i}{j}", f"n{i + di}{j + dj}"
                edges += [f'<edge id="{a}{b}" from="{a}" to="{b}"/>', f'<edge id="{b}{a}" from="{b}" to="{a}"/>']
    (d / "e.edg.xml").write_text("<edges>" + "".join(edges) + "</edges>")
    path = d / "grid.net.xml"
    subprocess.run(
        [netconvert, "-n", str(d / "n.nod.xml"), "-e", str(d / "e.edg.xml"), "--proj.utm", "-o", str(path)],
        check=True, capture_output=True,
    )
    return readNet(str(path))


def entity(n, heading=90, intensity=300, minutes_ago=10, location=(-0.37, 39.47)):
    e = {
        "id": f"urn:ngsi-ld:TrafficFlowObserved:pm_{n}",
        "type": "TrafficFlowObserved",
        "location": {"type": "Point", "coordinates": list(location)},
        "dateObserved": (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
    }
    if heading is not None:
        e["heading"] = heading
    if intensity is not None:
        e["intensity"] = intensity
    return e


def test_to_inputs_keeps_only_usable_recent_counts():
    entities = [
        entity(1),
        entity(2, heading=None),
        entity(3, intensity=None),
        entity(4, intensity=-1),
        entity(5, minutes_ago=200),
        {**entity(6), "location": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}},
        {**entity(7), "id": "urn:ngsi-ld:TrafficFlowObserved:tramo_A1"},
    ]
    detectors, flows, observed = dataspace_job.to_inputs(entities, NOW, timedelta(minutes=90))
    assert [d["detector_id"] for d in detectors] == ["1"]
    assert detectors[0]["heading"] == 90.0
    assert flows == [[1, 0, 300]]
    assert observed == NOW - timedelta(minutes=10)


def test_angle_diff_wraps():
    assert _angle_diff(350, 10) == 20
    assert _angle_diff(0, 180) == 180


def test_lane_bearing_matches_direction_of_travel(grid):
    # n00 -> n10 runs east, n10 -> n00 west (UTM grid convergence here is ~2 degrees)
    for edge_id, expected in (("n00n10", 90), ("n10n00", 270), ("n00n01", 0)):
        lane = grid.getEdge(edge_id).getLanes()[0]
        (x1, y1), (x2, y2) = lane.getShape()[0], lane.getShape()[-1]
        assert _angle_diff(_lane_bearing(lane, ((x1 + x2) / 2, (y1 + y2) / 2)), expected) < 3


def test_entity_key_is_uri_safe():
    assert sdm_output.entity_key("-123456#2") == "-123456~2"
    assert sdm_output.entity_key("a b/c") == "a%20b%2Fc"


def test_build_entities(grid, tmp_path):
    edges = [e for e in grid.getEdges() if not e.getID().startswith(":")]
    busy, empty = edges[0].getID(), edges[1].getID()
    edgedata = tmp_path / "edgedata.out.xml"
    edgedata.write_text(
        '<meandata><interval begin="0.00" end="3600.00" id="x">'
        f'<edge id="{busy}" entered="120" speed="10.0" occupancy="12.5"/>'
        f'<edge id="{empty}" entered="0" speed="13.9"/>'
        '<edge id=":internal_0" entered="50"/>'
        '</interval></meandata>'
    )
    emissions = tmp_path / "edge_emissions.out.xml"
    emissions.write_text(
        '<meandata><interval begin="0.00" end="3600.00" id="emissions">'
        f'<edge id="{busy}" CO2_abs="2500000000"/>'
        '</interval></meandata>'
    )
    start = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    flows, footprints = sdm_output.build_entities(
        grid, str(edgedata), str(emissions), start, "valencia", "VRAIN", "Simulated."
    )
    assert len(flows) == 1 and len(footprints) == 1
    flow, footprint = flows[0], footprints[0]
    assert flow["id"] == f"urn:ngsi-ld:TrafficFlowObserved:valencia:sim:{sdm_output.entity_key(busy)}"
    assert flow["intensity"]["value"] == 120
    assert flow["averageVehicleSpeed"]["value"] == 36.0
    assert flow["occupancy"]["value"] == 0.125
    assert flow["dateObservedFrom"]["value"] == "2026-11-18T10:00:00Z"
    assert flow["dateObservedTo"]["value"] == "2026-11-18T11:00:00Z"
    assert flow["location"]["value"]["type"] == "LineString"
    assert footprint["CO2eq"]["value"] == 2.5  # 2.5e9 mg = 2.5 t
    assert footprint["relatedSource"] == {"type": "Relationship", "object": flow["id"]}
    assert footprint["emissionSource"]["value"] == "Transport"


def test_detector_fit(tmp_path):
    observed = tmp_path / "obs.xml"
    observed.write_text('<data><interval begin="0" end="3600"><edge id="a" passenger="100"/>'
                        '<edge id="b" passenger="400"/></interval></data>')
    simulated = tmp_path / "sim.xml"
    simulated.write_text('<meandata><interval begin="0" end="3600"><edge id="a" entered="100"/>'
                         '<edge id="b" entered="200"/></interval></meandata>')
    fit = sdm_output.detector_fit(str(observed), str(simulated))
    assert fit == {"edges": 2, "geh_under_5": 0.5, "simulated_over_observed": 0.6}
