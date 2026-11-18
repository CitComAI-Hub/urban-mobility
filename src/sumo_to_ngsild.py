#!/usr/bin/env python3
"""
Converts a SUMO net.net.xml file to NGSI-LD entities.

Produces the following NGSI-LD entity types based on Smart Data Models:
  - ProjectionConfig : SUMO projection parameters (netOffset, projParameter, origBoundary)
  - Road             : one per non-internal edge (road segment group)
  - RoadSegment      : one per lane within non-internal edges
  - Junction         : one per junction (intersection / node)

Geo-coordinates are reprojected from SUMO's UTM offset system back to WGS84.
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from typing import Any

from pyproj import Transformer


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def parse_location(root: ET.Element) -> tuple[Transformer, float, float]:
    """Return a UTM→WGS84 transformer and the SUMO net offsets."""
    loc = root.find("location")
    if loc is None:
        sys.exit("ERROR: <location> element not found in the XML.")

    proj_param = loc.attrib["projParameter"]
    offset_x, offset_y = map(float, loc.attrib["netOffset"].split(","))

    transformer = Transformer.from_proj(proj_param, "epsg:4326", always_xy=True)
    return transformer, offset_x, offset_y


def sumo_to_wgs84(
    transformer: Transformer,
    offset_x: float,
    offset_y: float,
    sx: float,
    sy: float,
) -> tuple[float, float]:
    """Convert a SUMO (x, y) coordinate to (longitude, latitude)."""
    utm_x = sx - offset_x
    utm_y = sy - offset_y
    lon, lat = transformer.transform(utm_x, utm_y)
    return round(lon, 7), round(lat, 7)


def parse_shape(
    shape_str: str,
    transformer: Transformer,
    ox: float,
    oy: float,
) -> list[list[float]]:
    """Parse a SUMO shape string into a list of [lon, lat] pairs."""
    coords: list[list[float]] = []
    for point in shape_str.strip().split():
        parts = point.split(",")
        sx, sy = float(parts[0]), float(parts[1])
        lon, lat = sumo_to_wgs84(transformer, ox, oy, sx, sy)
        coords.append([lon, lat])
    return coords


# ---------------------------------------------------------------------------
# NGSI-LD entity builders
# ---------------------------------------------------------------------------

CONTEXT = "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context-v1.8.jsonld"


def _base_entity(entity_type: str, entity_id: str, domain: str = "") -> dict[str, Any]:
    if domain:
        full_id = f"urn:ngsi-ld:{entity_type}:{domain}:{entity_id}"
    else:
        full_id = f"urn:ngsi-ld:{entity_type}:{entity_id}"

    entity: dict[str, Any] = {
        "@context": CONTEXT,
        "id": full_id,
        "type": entity_type,
    }

    if domain:
        entity["domain"] = {"type": "Property", "value": domain}

    return entity


def build_projection_config_entity(root: ET.Element, domain: str = "") -> dict[str, Any]:
    """Build an NGSI-LD ProjectionConfig entity from the SUMO <location>."""
    loc = root.find("location")
    if loc is None:
        return {}

    entity = _base_entity("ProjectionConfig", "sumo-network", domain)

    entity["projParameter"] = {
        "type": "Property",
        "value": loc.attrib.get("projParameter", ""),
    }
    entity["netOffset"] = {
        "type": "Property",
        "value": loc.attrib.get("netOffset", ""),
    }
    entity["origBoundary"] = {
        "type": "Property",
        "value": loc.attrib.get("origBoundary", ""),
    }
    entity["convBoundary"] = {
        "type": "Property",
        "value": loc.attrib.get("convBoundary", ""),
    }

    return entity


def build_road_entity(
    edge: ET.Element,
    transformer: Transformer,
    ox: float,
    oy: float,
    road_types: dict[str, dict],
    domain: str = "",
) -> dict[str, Any]:
    """Build an NGSI-LD Road entity from a SUMO <edge>."""
    edge_id = edge.attrib["id"]
    entity = _base_entity("Road", edge_id, domain)

    entity["name"] = {"type": "Property", "value": edge.attrib.get("name", edge_id)}

    road_type = edge.attrib.get("type", "")
    if road_type:
        entity["roadClass"] = {"type": "Property", "value": road_type}

    type_info = road_types.get(road_type, {})
    if "priority" in type_info:
        entity["priority"] = {"type": "Property", "value": int(type_info["priority"])}

    lanes = edge.findall("lane")
    if lanes:
        entity["laneCount"] = {"type": "Property", "value": len(lanes)}
        max_speed = max(float(l.attrib.get("speed", 0)) for l in lanes)
        entity["maximumAllowedSpeed"] = {
            "type": "Property",
            "value": round(max_speed * 3.6, 2),
            "unitCode": "KMH",
        }

    shape_str = edge.attrib.get("shape", "")
    if not shape_str and lanes:
        shape_str = lanes[0].attrib.get("shape", "")
    if shape_str:
        coords = parse_shape(shape_str, transformer, ox, oy)
        entity["location"] = {
            "type": "GeoProperty",
            "value": {"type": "LineString", "coordinates": coords},
        }

    if "from" in edge.attrib:
        pfx = f"urn:ngsi-ld:Junction:{domain}:" if domain else "urn:ngsi-ld:Junction:"
        entity["startJunction"] = {
            "type": "Relationship",
            "object": f"{pfx}{edge.attrib['from']}",
        }
    if "to" in edge.attrib:
        pfx = f"urn:ngsi-ld:Junction:{domain}:" if domain else "urn:ngsi-ld:Junction:"
        entity["endJunction"] = {
            "type": "Relationship",
            "object": f"{pfx}{edge.attrib['to']}",
        }

    return entity


def build_road_segment_entity(
    lane: ET.Element,
    edge_id: str,
    transformer: Transformer,
    ox: float,
    oy: float,
    domain: str = "",
) -> dict[str, Any]:
    """Build an NGSI-LD RoadSegment entity from a SUMO <lane>."""
    lane_id = lane.attrib["id"]
    entity = _base_entity("RoadSegment", lane_id, domain)

    entity["name"] = {"type": "Property", "value": lane_id}
    pfx = f"urn:ngsi-ld:Road:{domain}:" if domain else "urn:ngsi-ld:Road:"
    entity["refRoad"] = {
        "type": "Relationship",
        "object": f"{pfx}{edge_id}",
    }
    entity["laneId"] = {
        "type": "Property",
        "value": int(lane.attrib.get("index", 0)),
    }

    speed = float(lane.attrib.get("speed", 0))
    entity["maximumAllowedSpeed"] = {
        "type": "Property",
        "value": round(speed * 3.6, 2),
        "unitCode": "KMH",
    }

    length = float(lane.attrib.get("length", 0))
    entity["length"] = {
        "type": "Property",
        "value": round(length, 2),
        "unitCode": "MTR",
    }

    if "width" in lane.attrib:
        entity["width"] = {
            "type": "Property",
            "value": float(lane.attrib["width"]),
            "unitCode": "MTR",
        }

    allow = lane.attrib.get("allow", "")
    if allow:
        entity["allowedVehicleType"] = {
            "type": "Property",
            "value": allow.split(),
        }
    disallow = lane.attrib.get("disallow", "")
    if disallow:
        entity["disallowedVehicleType"] = {
            "type": "Property",
            "value": disallow.split(),
        }

    shape_str = lane.attrib.get("shape", "")
    if shape_str:
        coords = parse_shape(shape_str, transformer, ox, oy)
        entity["location"] = {
            "type": "GeoProperty",
            "value": {"type": "LineString", "coordinates": coords},
        }

    return entity


def build_junction_entity(
    junction: ET.Element,
    transformer: Transformer,
    ox: float,
    oy: float,
    domain: str = "",
) -> dict[str, Any]:
    """Build an NGSI-LD Junction entity from a SUMO <junction>."""
    jid = junction.attrib["id"]
    entity = _base_entity("Junction", jid, domain)

    entity["name"] = {"type": "Property", "value": jid}
    entity["junctionType"] = {
        "type": "Property",
        "value": junction.attrib.get("type", "unknown"),
    }

    sx = float(junction.attrib.get("x", 0))
    sy = float(junction.attrib.get("y", 0))
    lon, lat = sumo_to_wgs84(transformer, ox, oy, sx, sy)
    entity["location"] = {
        "type": "GeoProperty",
        "value": {"type": "Point", "coordinates": [lon, lat]},
    }

    if "z" in junction.attrib:
        entity["elevation"] = {
            "type": "Property",
            "value": float(junction.attrib["z"]),
            "unitCode": "MTR",
        }

    inc_lanes = junction.attrib.get("incLanes", "").split()
    if inc_lanes and inc_lanes != [""]:
        entity["incomingLanes"] = {
            "type": "Property",
            "value": inc_lanes,
        }

    int_lanes = junction.attrib.get("intLanes", "").split()
    if int_lanes and int_lanes != [""]:
        entity["internalLanes"] = {
            "type": "Property",
            "value": int_lanes,
        }

    shape_str = junction.attrib.get("shape", "")
    if shape_str:
        coords = parse_shape(shape_str, transformer, ox, oy)
        if len(coords) >= 3:
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            entity["area"] = {
                "type": "GeoProperty",
                "value": {"type": "Polygon", "coordinates": [coords]},
            }

    return entity


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(input_path: str, output_path: str, include_internal: bool = False, domain: str = "") -> None:
    """Parse the SUMO net XML and write NGSI-LD entities as JSON."""
    tree = ET.parse(input_path)
    root = tree.getroot()

    transformer, ox, oy = parse_location(root)

    # Collect road type metadata
    road_types: dict[str, dict] = {}
    for t in root.findall("type"):
        road_types[t.attrib["id"]] = t.attrib

    entities: list[dict[str, Any]] = []

    # ProjectionConfig (projection parameters for roundtrip)
    proj_entity = build_projection_config_entity(root, domain)
    if proj_entity:
        entities.append(proj_entity)

    # Edges → Road + RoadSegment
    for edge in root.findall("edge"):
        edge_id = edge.attrib["id"]
        is_internal = edge.attrib.get("function") == "internal" or edge_id.startswith(":")
        if is_internal and not include_internal:
            continue

        entities.append(build_road_entity(edge, transformer, ox, oy, road_types, domain))
        for lane in edge.findall("lane"):
            entities.append(
                build_road_segment_entity(lane, edge_id, transformer, ox, oy, domain)
            )

    # Junctions
    for junction in root.findall("junction"):
        jtype = junction.attrib.get("type", "")
        if jtype == "internal" and not include_internal:
            continue
        entities.append(build_junction_entity(junction, transformer, ox, oy, domain))

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(entities, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(entities)} NGSI-LD entities to {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a SUMO net.net.xml to NGSI-LD JSON entities."
    )
    parser.add_argument("input", help="Path to the SUMO net.net.xml file")
    parser.add_argument(
        "-o", "--output",
        default="ngsild_entities.json",
        help="Output JSON file (default: ngsild_entities.json)",
    )
    parser.add_argument(
        "--include-internal",
        action="store_true",
        help="Include internal edges and junctions (default: skip them)",
    )
    parser.add_argument(
        "--domain",
        default="",
        help="Domain identifier to tag all entities (e.g. 'hikvision_cameras_347')",
    )
    args = parser.parse_args()
    convert(args.input, args.output, args.include_internal, args.domain)


if __name__ == "__main__":
    main()
