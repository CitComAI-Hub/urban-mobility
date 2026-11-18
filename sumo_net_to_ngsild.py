#!/usr/bin/env python3
"""
Converts a SUMO net.net.xml file to NGSI-LD entities (lossless).

Produces the following NGSI-LD entity types:
  - SumoNetworkConfig : net root attributes + location element (projection, offsets, boundaries)
  - SumoEdgeType      : one per <type> element
  - SumoEdge          : one per <edge> (including internal, crossing, walkingarea)
  - SumoLane          : one per <lane> within an edge
  - SumoJunction      : one per <junction> (including internal)
  - SumoRequest       : one per <request> within a junction
  - SumoConnection    : one per <connection>
  - SumoTlLogic      : one per <tlLogic> element (traffic light programs)

All shape coordinates are stored *verbatim* (as SUMO local strings) to guarantee
bit-exact round-trip conversion.  WGS84 GeoProperties are added alongside for
geospatial queries but are NOT used in the inverse conversion.
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from typing import Any

from pyproj import Transformer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTEXT = "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context-v1.8.jsonld"


# ---------------------------------------------------------------------------
# Geo helpers (for informational WGS84 GeoProperties only)
# ---------------------------------------------------------------------------

def _parse_location(root: ET.Element) -> tuple[Transformer, float, float] | None:
    loc = root.find("location")
    if loc is None:
        return None
    proj_param = loc.attrib.get("projParameter", "")
    if not proj_param or proj_param == "!":
        return None
    offset_x, offset_y = map(float, loc.attrib["netOffset"].split(","))
    transformer = Transformer.from_proj(proj_param, "epsg:4326", always_xy=True)
    return transformer, offset_x, offset_y


def _sumo_to_wgs84(
    transformer: Transformer, ox: float, oy: float, sx: float, sy: float,
) -> tuple[float, float]:
    utm_x = sx - ox
    utm_y = sy - oy
    lon, lat = transformer.transform(utm_x, utm_y)
    return round(lon, 7), round(lat, 7)


def _shape_to_geo(
    shape_str: str, transformer: Transformer, ox: float, oy: float,
) -> list[list[float]]:
    coords: list[list[float]] = []
    for point in shape_str.strip().split():
        parts = point.split(",")
        sx, sy = float(parts[0]), float(parts[1])
        lon, lat = _sumo_to_wgs84(transformer, ox, oy, sx, sy)
        coords.append([lon, lat])
    return coords


# ---------------------------------------------------------------------------
# Entity helpers
# ---------------------------------------------------------------------------

def _prop(value: Any, unit: str | None = None) -> dict:
    p: dict[str, Any] = {"type": "Property", "value": value}
    if unit:
        p["unitCode"] = unit
    return p


def _geo_point(lon: float, lat: float) -> dict:
    return {
        "type": "GeoProperty",
        "value": {"type": "Point", "coordinates": [lon, lat]},
    }


def _geo_line(coords: list[list[float]]) -> dict:
    return {
        "type": "GeoProperty",
        "value": {"type": "LineString", "coordinates": coords},
    }


def _rel(target_type: str, target_id: str, domain: str) -> dict:
    pfx = f"urn:ngsi-ld:{target_type}:{domain}:" if domain else f"urn:ngsi-ld:{target_type}:"
    return {"type": "Relationship", "object": f"{pfx}{target_id}"}


def _base(entity_type: str, entity_id: str, domain: str) -> dict[str, Any]:
    pfx = f"urn:ngsi-ld:{entity_type}:{domain}:" if domain else f"urn:ngsi-ld:{entity_type}:"
    entity: dict[str, Any] = {
        "@context": CONTEXT,
        "id": f"{pfx}{entity_id}",
        "type": entity_type,
    }
    if domain:
        entity["domain"] = _prop(domain)
    return entity


# ---------------------------------------------------------------------------
# Entity builders
# ---------------------------------------------------------------------------

def _extract_net_tag(input_path: str) -> str:
    """Extract the raw <net ...> opening tag from the file to preserve xmlns attributes."""
    import re as _re
    with open(input_path, encoding="utf-8") as f:
        buf = ""
        for line in f:
            buf += line
            # Match <net> or <net ...> but NOT <netconvert...> or other <net*> tags
            m = _re.search(r"<net(?:\s[^>]*)?>", buf)
            if m:
                return m.group(0)
    return ""


def build_network_config(
    root: ET.Element, domain: str, net_tag: str = "",
) -> dict[str, Any]:
    """Build a SumoNetworkConfig entity from <net> root + <location>."""
    entity = _base("SumoNetworkConfig", "main", domain)

    # Store raw <net> tag to preserve xmlns attributes exactly
    if net_tag:
        entity["netTag"] = _prop(net_tag)

    # <location> attributes
    loc = root.find("location")
    if loc is not None:
        for attr in ("netOffset", "convBoundary", "origBoundary", "projParameter"):
            val = loc.attrib.get(attr, "")
            entity[attr] = _prop(val)

    return entity


def build_edge_type(type_elem: ET.Element, domain: str) -> dict[str, Any]:
    """Build a SumoEdgeType entity from a <type> element."""
    tid = type_elem.attrib["id"]
    entity = _base("SumoEdgeType", tid, domain)

    raw_attrs = [[k, v] for k, v in type_elem.attrib.items() if k != "id"]
    entity["rawAttrs"] = _prop(raw_attrs)

    return entity


def build_edge(
    edge: ET.Element, domain: str,
    geo_info: tuple[Transformer, float, float] | None,
) -> dict[str, Any]:
    """Build a SumoEdge entity from an <edge> element."""
    edge_id = edge.attrib["id"]
    entity = _base("SumoEdge", edge_id, domain)

    # Store all attributes in original order (excluding "id")
    raw_attrs = [[k, v] for k, v in edge.attrib.items() if k != "id"]
    entity["rawAttrs"] = _prop(raw_attrs)

    # Add WGS84 GeoProperty for geospatial queries
    shape = edge.attrib.get("shape")
    if shape and geo_info:
        coords = _shape_to_geo(shape, *geo_info)
        if len(coords) >= 2:
            entity["location"] = _geo_line(coords)

    # <param> children
    params = edge.findall("param")
    if params:
        param_list = [{"key": p.attrib["key"], "value": p.attrib.get("value", "")} for p in params]
        entity["params"] = _prop(param_list)

    return entity


def build_lane(
    lane: ET.Element, edge_id: str, domain: str,
    geo_info: tuple[Transformer, float, float] | None,
) -> dict[str, Any]:
    """Build a SumoLane entity from a <lane> element."""
    lane_id = lane.attrib["id"]
    entity = _base("SumoLane", lane_id, domain)

    entity["refEdge"] = _rel("SumoEdge", edge_id, domain)

    # Store all attributes in original order as key-value pairs
    raw_attrs = [[k, v] for k, v in lane.attrib.items() if k != "id"]
    entity["rawAttrs"] = _prop(raw_attrs)

    # Add WGS84 GeoProperty for geospatial queries
    shape = lane.attrib.get("shape")
    if shape and geo_info:
        coords = _shape_to_geo(shape, *geo_info)
        if len(coords) >= 2:
            entity["location"] = _geo_line(coords)

    return entity


def build_junction(
    junction: ET.Element, domain: str,
    geo_info: tuple[Transformer, float, float] | None,
) -> dict[str, Any]:
    """Build a SumoJunction entity from a <junction> element."""
    jid = junction.attrib["id"]
    entity = _base("SumoJunction", jid, domain)

    # Store all attributes in original order (excluding "id")
    raw_attrs = [[k, v] for k, v in junction.attrib.items() if k != "id"]
    entity["rawAttrs"] = _prop(raw_attrs)

    # Add WGS84 point for geospatial queries
    if geo_info and "x" in junction.attrib and "y" in junction.attrib:
        sx = float(junction.attrib["x"])
        sy = float(junction.attrib["y"])
        lon, lat = _sumo_to_wgs84(*geo_info, sx, sy)
        entity["location"] = _geo_point(lon, lat)

    # <param> children
    params = junction.findall("param")
    if params:
        param_list = [{"key": p.attrib["key"], "value": p.attrib.get("value", "")} for p in params]
        entity["params"] = _prop(param_list)

    return entity


def build_request(
    req: ET.Element, junction_id: str, domain: str,
) -> dict[str, Any]:
    """Build a SumoRequest entity from a <request> element inside a junction."""
    idx = req.attrib["index"]
    entity_id = f"{junction_id}_req{idx}"
    entity = _base("SumoRequest", entity_id, domain)

    entity["refJunction"] = _rel("SumoJunction", junction_id, domain)

    # Store all attributes in original order
    raw_attrs = [[k, v] for k, v in req.attrib.items()]
    entity["rawAttrs"] = _prop(raw_attrs)

    return entity


def build_tl_logic(
    tl: ET.Element, domain: str,
) -> dict[str, Any]:
    """Build a SumoTlLogic entity from a <tlLogic> element."""
    tid = tl.attrib["id"]
    entity = _base("SumoTlLogic", tid, domain)

    raw_attrs = [[k, v] for k, v in tl.attrib.items() if k != "id"]
    entity["rawAttrs"] = _prop(raw_attrs)

    phases = tl.findall("phase")
    if phases:
        entity["phases"] = _prop([dict(p.attrib) for p in phases])

    return entity


def build_connection(
    conn: ET.Element, conn_index: int, domain: str,
) -> dict[str, Any]:
    """Build a SumoConnection entity from a <connection> element."""
    entity_id = f"conn_{conn_index}"
    entity = _base("SumoConnection", entity_id, domain)

    # Store all attributes in original order
    raw_attrs = [[k, v] for k, v in conn.attrib.items()]
    entity["rawAttrs"] = _prop(raw_attrs)

    return entity


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(
    input_path: str,
    output_path: str,
    domain: str = "",
) -> None:
    """Parse SUMO net XML and write all elements as NGSI-LD entities."""
    tree = ET.parse(input_path)
    root = tree.getroot()

    geo_info = _parse_location(root)
    entities: list[dict[str, Any]] = []

    # 1. Network config (location + net attributes + raw <net> tag)
    net_tag = _extract_net_tag(input_path)
    entities.append(build_network_config(root, domain, net_tag))

    # 2. Edge types
    for t in root.findall("type"):
        entities.append(build_edge_type(t, domain))

    # 3. Edges + Lanes (ALL, including internal/crossing/walkingarea)
    for edge in root.findall("edge"):
        edge_id = edge.attrib["id"]
        entities.append(build_edge(edge, domain, geo_info))
        for lane in edge.findall("lane"):
            entities.append(build_lane(lane, edge_id, domain, geo_info))

    # 4. Traffic light logic
    for tl in root.findall("tlLogic"):
        entities.append(build_tl_logic(tl, domain))

    # 5. Junctions + Requests (ALL, including internal)
    for junction in root.findall("junction"):
        jid = junction.attrib["id"]
        entities.append(build_junction(junction, domain, geo_info))
        for req in junction.findall("request"):
            entities.append(build_request(req, jid, domain))

    # 6. Connections
    for i, conn in enumerate(root.findall("connection")):
        entities.append(build_connection(conn, i, domain))

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(entities, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(entities)} NGSI-LD entities to {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a SUMO net.net.xml to NGSI-LD JSON (lossless).",
    )
    parser.add_argument("input", help="Path to the SUMO net.net.xml file")
    parser.add_argument(
        "-o", "--output",
        default="ngsild_network.json",
        help="Output JSON file (default: ngsild_network.json)",
    )
    parser.add_argument(
        "--domain",
        default="",
        help="Domain identifier to tag all entities (e.g. 'hikvision_cameras_347')",
    )
    args = parser.parse_args()
    convert(args.input, args.output, args.domain)


if __name__ == "__main__":
    main()
