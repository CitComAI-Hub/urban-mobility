#!/usr/bin/env python3
"""
CLI wrapper for NgsildToSumoNet.

Converts a NGSI-LD JSON file (produced by sumo_net_to_ngsild.py) back to a
SUMO net.net.xml file.  All conversion logic lives in src/ngsild_to_sumo_net.py.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from src.ngsild_to_sumo_net import NgsildToSumoNet


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert NGSI-LD JSON entities to a SUMO net.net.xml (lossless).",
    )
    parser.add_argument("input", help="Path to the NGSI-LD JSON file")
    parser.add_argument(
        "-o", "--output",
        default="output.net.xml",
        help="Output SUMO net.xml file (default: output.net.xml)",
    )
    args = parser.parse_args()

    NgsildToSumoNet.from_file(args.input).write(args.output)


if __name__ == "__main__":
    main()
