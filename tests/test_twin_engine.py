"""Twin engine without simulation: zone loading and re-routing around closed streets."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "twin"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import engine

ZONE = os.path.join(os.path.dirname(__file__), "..", "data", "valencia", "zones", "camins-al-grau")


def zone():
    return engine.Zone(os.path.join(ZONE, "net.xml.gz"), os.path.join(ZONE, "candidate.rou.xml.gz"), "test",
                       [-0.34787, 39.45781, -0.31798, 39.47964])


def test_fastest_avoids_closed_edges():
    z = zone()
    route = None
    for vid, _, edges in engine._read_vehicles_from_candidates(z.routes_file, limit=200):
        if len(edges) > 6:
            route = edges
            break
    assert route is not None
    start, end, middle = route[0], route[-1], route[len(route) // 2]
    free = z.fastest(start, end, set())
    assert free and free[0] == start and free[-1] == end
    detour = z.fastest(start, end, {middle})
    assert detour is None or middle not in detour
    assert z.fastest(start, end, {start}) is None


def test_inside_uses_zone_bbox():
    z = zone()
    assert z.inside(-0.33, 39.47)
    assert not z.inside(-0.376, 39.47)  # Plaça de l'Ajuntament, outside the zone
