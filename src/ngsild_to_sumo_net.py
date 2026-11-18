"""
Converts NGSI-LD entities back to a SUMO net.net.xml file (lossless).

Reads a list of NGSI-LD entity dicts produced by sumo_net_to_ngsild.py
and reconstructs a bit-exact copy of the original SUMO network XML.

Entity types consumed:
  - SumoNetworkConfig
  - SumoEdgeType
  - SumoEdge + SumoLane
  - SumoTlLogic
  - SumoJunction + SumoRequest
  - SumoConnection

Usage as a library::

    from src.ngsild_to_sumo_net import NgsildToSumoNet

    converter = NgsildToSumoNet(entities)          # list[dict]
    xml_str   = converter.to_xml()                 # str
    converter.write("output.net.xml")              # write to file
"""

import json
from typing import Any


# ---------------------------------------------------------------------------
# NGSI-LD property helpers (module-level, reusable)
# ---------------------------------------------------------------------------

def _prop(entity: dict, key: str, default: Any = None) -> Any:
    p = entity.get(key)
    if p is None:
        return default
    return p.get("value", default)


def _rel_local_id(entity: dict, key: str) -> str | None:
    r = entity.get(key)
    if r is None:
        return None
    obj = r.get("object", "")
    parts = obj.split(":", 3)
    if len(parts) < 4:
        return obj
    remainder = parts[3]
    domain = _prop(entity, "domain", "")
    if domain and remainder.startswith(domain + ":"):
        return remainder[len(domain) + 1:]
    return remainder


def _entity_local_id(entity: dict) -> str:
    eid = entity.get("id", "")
    parts = eid.split(":", 3)
    if len(parts) < 4:
        return eid
    remainder = parts[3]
    domain = _prop(entity, "domain", "")
    if domain and remainder.startswith(domain + ":"):
        return remainder[len(domain) + 1:]
    return remainder


# ---------------------------------------------------------------------------
# XML serialiser helpers
# ---------------------------------------------------------------------------

_INDENT = "    "


def _attr_str(attribs: list[list[str]] | dict[str, str]) -> str:
    """Format attributes as key="value" pairs, preserving order."""
    if isinstance(attribs, dict):
        items = attribs.items()
    else:
        items = attribs
    return " ".join(f'{k}="{v}"' for k, v in items)


def _write_element(
    lines: list[str],
    tag: str,
    attribs: list[list[str]] | dict[str, str],
    children: list[tuple[str, list[list[str]] | dict[str, str]]] | None = None,
    depth: int = 1,
) -> None:
    """Append an XML element (self-closing or with children) to *lines*."""
    indent = _INDENT * depth
    if children:
        lines.append(f"{indent}<{tag} {_attr_str(attribs)}>")
        for ctag, cattribs in children:
            child_indent = _INDENT * (depth + 1)
            lines.append(f"{child_indent}<{ctag} {_attr_str(cattribs)}/>")
        lines.append(f"{indent}</{tag}>")
    else:
        lines.append(f"{indent}<{tag} {_attr_str(attribs)}/>")


def _raw_attrs(entity: dict) -> list[list[str]]:
    return _prop(entity, "rawAttrs", [])


def _raw_attr_val(entity: dict, key: str, default: str = "0") -> str:
    for k, v in _raw_attrs(entity):
        if k == key:
            return v
    return default


# ---------------------------------------------------------------------------
# Converter class
# ---------------------------------------------------------------------------

class NgsildToSumoNet:
    """
    Converts a list of NGSI-LD entities (produced by sumo_net_to_ngsild.py)
    back to a SUMO net.xml string.

    Parameters
    ----------
    entities:
        List of NGSI-LD entity dicts (as returned by ``json.load``).
    """

    def __init__(self, entities: list[dict]) -> None:
        self._entities = entities
        self._indexed = False

        # Indexed structures (populated lazily on first call to to_xml)
        self._network_config: dict | None = None
        self._edge_types: list[dict] = []
        self._edges: dict[str, dict] = {}
        self._edge_order: list[str] = []
        self._lanes: dict[str, list[dict]] = {}
        self._tl_logics: list[dict] = []
        self._junctions: dict[str, dict] = {}
        self._junction_order: list[str] = []
        self._requests: dict[str, list[dict]] = {}
        self._connections: list[dict] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def to_xml(self) -> str:
        """Return the reconstructed SUMO net.xml as a string."""
        self._index()
        return self._build_xml()

    def write(self, output_path: str) -> None:
        """Write the reconstructed SUMO net.xml to *output_path*."""
        xml_content = self.to_xml()
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(xml_content)
        print(f"Wrote SUMO network to {output_path}")

    # ------------------------------------------------------------------
    # Class methods for common loading patterns
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, input_path: str) -> "NgsildToSumoNet":
        """Load entities from a JSON file and return a converter instance."""
        with open(input_path, encoding="utf-8") as f:
            entities = json.load(f)
        return cls(entities)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _index(self) -> None:
        """Parse the flat entity list into typed, ordered collections."""
        if self._indexed:
            return

        for e in self._entities:
            etype = e.get("type")
            eid = _entity_local_id(e)

            if etype == "SumoNetworkConfig":
                self._network_config = e
            elif etype == "SumoEdgeType":
                self._edge_types.append(e)
            elif etype == "SumoEdge":
                self._edges[eid] = e
                self._edge_order.append(eid)
            elif etype == "SumoLane":
                edge_id = _rel_local_id(e, "refEdge")
                if edge_id:
                    self._lanes.setdefault(edge_id, []).append(e)
            elif etype == "SumoTlLogic":
                self._tl_logics.append(e)
            elif etype == "SumoJunction":
                self._junctions[eid] = e
                self._junction_order.append(eid)
            elif etype == "SumoRequest":
                junc_id = _rel_local_id(e, "refJunction")
                if junc_id:
                    self._requests.setdefault(junc_id, []).append(e)
            elif etype == "SumoConnection":
                self._connections.append(e)

        # Sort lanes by index within each edge
        for lane_list in self._lanes.values():
            lane_list.sort(key=lambda l: int(_raw_attr_val(l, "index", "0")))

        # Sort requests by index within each junction
        for req_list in self._requests.values():
            req_list.sort(key=lambda r: int(_raw_attr_val(r, "index", "0")))

        # Sort connections to restore original order (conn_0, conn_1, ...)
        self._connections.sort(
            key=lambda c: int(_entity_local_id(c).replace("conn_", ""))
        )

        self._indexed = True

    def _build_xml(self) -> str:
        lines: list[str] = []
        lines.append('<?xml version="1.0" encoding="UTF-8"?>')
        lines.append("")

        # <net> root tag
        net_tag = _prop(self._network_config, "netTag", "") if self._network_config else ""
        lines.append(net_tag if net_tag else '<net version="1.20">')
        lines.append("")

        # <location>
        if self._network_config:
            loc_attribs: dict[str, str] = {}
            for attr in ("netOffset", "convBoundary", "origBoundary", "projParameter"):
                val = _prop(self._network_config, attr, "")
                loc_attribs[attr] = str(val)
            _write_element(lines, "location", loc_attribs)
        lines.append("")

        # <type> elements
        for t in self._edge_types:
            tid = _entity_local_id(t)
            _write_element(lines, "type", [["id", tid]] + _raw_attrs(t))
        if self._edge_types:
            lines.append("")

        # <edge> elements with <lane> and <param> children
        self._write_edges(lines)
        lines.append("")

        # <tlLogic> elements with <phase> children
        self._write_tl_logics(lines)
        if self._tl_logics:
            lines.append("")

        # <junction> elements with <request> and <param> children
        self._write_junctions(lines)
        lines.append("")

        # <connection> elements
        self._write_connections(lines)

        lines.append("</net>")
        return "\n".join(lines) + "\n"

    def _write_edges(self, lines: list[str]) -> None:
        prev_is_internal = None
        for edge_id in self._edge_order:
            edge = self._edges[edge_id]
            is_internal = edge_id.startswith(":")

            if prev_is_internal is not None and prev_is_internal and not is_internal:
                lines.append("")
            prev_is_internal = is_internal

            eattribs = [["id", edge_id]] + _raw_attrs(edge)

            children: list[tuple[str, list[list[str]]]] = []
            for lane_ent in self._lanes.get(edge_id, []):
                lid = _entity_local_id(lane_ent)
                children.append(("lane", [["id", lid]] + _raw_attrs(lane_ent)))

            edge_params = _prop(edge, "params")
            if edge_params:
                for p in edge_params:
                    children.append(("param", [["key", p["key"]], ["value", p["value"]]]))

            _write_element(lines, "edge", eattribs, children if children else None)

    def _write_tl_logics(self, lines: list[str]) -> None:
        for tl in self._tl_logics:
            tlid = _entity_local_id(tl)
            tlattribs = [["id", tlid]] + _raw_attrs(tl)
            phases = _prop(tl, "phases", [])
            if phases:
                # SUMO pads duration so all `state=` attributes column-align
                max_dur_len = max(len(p.get("duration", "")) for p in phases)
                indent = _INDENT
                lines.append(f"{indent}<tlLogic {_attr_str(tlattribs)}>")
                for p in phases:
                    dur = p.get("duration", "")
                    padding = " " * (max_dur_len - len(dur))
                    other_str = " ".join(
                        f'{k}="{v}"' for k, v in p.items() if k != "duration"
                    )
                    lines.append(f'{indent}{indent}<phase duration="{dur}"{padding} {other_str}/>')
                lines.append(f"{indent}</tlLogic>")
            else:
                _write_element(lines, "tlLogic", tlattribs)

    def _write_junctions(self, lines: list[str]) -> None:
        prev_is_internal = None
        for jid in self._junction_order:
            junc = self._junctions[jid]
            is_internal = jid.startswith(":")

            if prev_is_internal is not None and not prev_is_internal and is_internal:
                lines.append("")
            prev_is_internal = is_internal

            jattribs = [["id", jid]] + _raw_attrs(junc)
            req_list = self._requests.get(jid, [])
            junc_params = _prop(junc, "params", [])

            if req_list or junc_params:
                indent = _INDENT
                lines.append(f"{indent}<junction {_attr_str(jattribs)}>")
                if req_list:
                    max_idx_len = max(len(_raw_attr_val(r, "index", "0")) for r in req_list)
                    for req in req_list:
                        attrs = _raw_attrs(req)
                        parts = []
                        for k, v in attrs:
                            parts.append(f'{k}="{v}"')
                            if k == "index" and len(v) < max_idx_len:
                                parts[-1] += " " * (max_idx_len - len(v))
                        lines.append(f"{indent}{indent}<request {' '.join(parts)}/>")
                for p in junc_params:
                    lines.append(
                        f'{indent}{indent}<param key="{p["key"]}" value="{p["value"]}"/>'
                    )
                lines.append(f"{indent}</junction>")
            else:
                _write_element(lines, "junction", jattribs)

    def _write_connections(self, lines: list[str]) -> None:
        def _conn_group(attrs: list[list[str]]) -> int:
            conn_from = dict(attrs).get("from", "")
            conn_to = dict(attrs).get("to", "")
            if not conn_from.startswith(":"):
                if conn_to.startswith(":") and "_w" in conn_to:
                    return 2
                return 0
            if "_w" in conn_from or "_c" in conn_from:
                return 2
            return 1

        prev_group = None
        for conn in self._connections:
            attrs = _raw_attrs(conn)
            group = _conn_group(attrs)
            if prev_group is not None and group > prev_group:
                lines.append("")
            prev_group = group
            _write_element(lines, "connection", attrs)
