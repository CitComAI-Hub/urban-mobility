"""Prior for routeSampler: weight the candidate routes by a plausible origin–destination.

    python tools/calibration/od_prior.py <zone_net> <candidate_routes> <ghs_pop.tif> <out.rou.xml.gz> [internal_share]

Without a prior, routeSampler picks any combination of random routes that matches the
counts, and the flows on streets without counts are arbitrary. Here each candidate route
gets probability = w(origin edge) * w(destination edge), and routeSampler --weighted
samples routes by it:
- edges where traffic enters/leaves the zone (no predecessor/successor inside it):
  lanes x road-class factor (through traffic uses the big roads);
- internal edges: residents around the edge (GHS-POP cells within RADIUS m of its middle),
  as trips start and end where people live;
- internal_share (0.4): share of origins (and of destinations) that are internal.
No counts are used, so a hold-out validation stays clean.
"""
import gzip
import math
import sys
import xml.etree.ElementTree as ET

import numpy as np
import rasterio
import sumolib

RADIUS = 150
CLASS = {"motorway": 3, "trunk": 3, "primary": 2, "secondary": 1.5, "tertiary": 1, "residential": 0.3,
         "living_street": 0.1, "service": 0.1, "unclassified": 0.5}


def main():
    net_file, routes_file, tif, out, *rest = sys.argv[1:]
    share = float(rest[0]) if rest else 0.4
    net = sumolib.net.readNet(net_file)
    (x0, y0), (x1, y1) = net.getBBoxXY()
    lon0, lat0 = net.convertXY2LonLat(x0, y0)
    lon1, lat1 = net.convertXY2LonLat(x1, y1)
    with rasterio.open(tif) as r:
        win = rasterio.windows.from_bounds(lon0 - 0.01, lat0 - 0.01, lon1 + 0.01, lat1 + 0.01, r.transform)
        pop = r.read(1, window=win).astype(float)
        tr = r.window_transform(win)
    pop[pop < 0] = 0
    cell_r = int(math.ceil(RADIUS / 90))

    def residents(edge):
        s = edge.getShape()
        lon, lat = net.convertXY2LonLat(*s[len(s) // 2])
        c, rw = ~tr * (lon, lat)
        c, rw = int(c), int(rw)
        return float(pop[max(rw - cell_r, 0):rw + cell_r + 1, max(c - cell_r, 0):c + cell_r + 1].sum())

    def road(edge):
        return edge.getLaneNumber() * CLASS.get((edge.getType() or "").split(".")[-1], 0.5)

    o_w, d_w = {}, {}
    for e in net.getEdges():
        if e.isSpecial() or not e.allows("passenger"):
            continue
        entry = not [i for i in e.getIncoming() if not i.isSpecial()]
        exit_ = not [o for o in e.getOutgoing() if not o.isSpecial()]
        o_w[e.getID()] = ("b", road(e)) if entry else ("i", residents(e))
        d_w[e.getID()] = ("b", road(e)) if exit_ else ("i", residents(e))

    def normalise(w):
        tb = sum(v for k, v in w.values() if k == "b") or 1
        ti = sum(v for k, v in w.values() if k == "i") or 1
        return {e: (v / tb * (1 - share) if k == "b" else v / ti * share) for e, (k, v) in w.items()}

    o_n, d_n = normalise(o_w), normalise(d_w)
    n = kept = 0
    with gzip.open(out, "wt") as f:
        f.write("<routes>\n")
        for _, el in ET.iterparse(gzip.open(routes_file) if routes_file.endswith(".gz") else routes_file):
            if el.tag == "route" and el.get("edges"):
                edges = el.get("edges").split()
                p = o_n.get(edges[0], 0) * d_n.get(edges[-1], 0)
                n += 1
                if p > 0:
                    kept += 1
                    f.write(f'  <route id="r{kept}" edges="{" ".join(edges)}" probability="{p * 1e6:.6g}"/>\n')
            elif el.tag == "vehicle":
                el.clear()
        f.write("</routes>\n")
    print(f"{kept} of {n} routes with a prior weight; internal share {share}")


if __name__ == "__main__":
    main()
