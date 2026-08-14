"""Thin argument parsing and command dispatch."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from . import generate_inventory


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ceph-incident-bundle")
    subcommands = parser.add_subparsers(dest="subcommand", required=True)

    generate_parser = subcommands.add_parser(
        "generate-inventory", help="draft a Node Inventory from a hosts file"
    )
    generate_parser.add_argument("--hosts-file", type=Path, default=Path("/etc/hosts"))
    generate_parser.add_argument("--output", type=Path, default=Path("inventory.ini"))
    generate_parser.add_argument("--force", action="store_true")

    collect_parser = subcommands.add_parser(
        "collect", help="collect an Incident Bundle from a Node Inventory"
    )
    collect_parser.add_argument("--inventory", type=Path, default=Path("inventory.ini"))
    collect_parser.add_argument("--since", default="24h")
    collect_parser.add_argument("--output-dir", type=Path, default=Path.cwd())

    arguments = parser.parse_args(argv)
    if arguments.subcommand == "generate-inventory":
        return generate_inventory.run(
            arguments.hosts_file, arguments.output, arguments.force
        )

    try:
        from .collect import run as collect
    except ModuleNotFoundError as error:
        if error.name != "ceph_incident_bundle.collect":
            raise
        print(
            "collect is not available in this preparation-only build",
            file=sys.stderr,
        )
        print("FAIL: no Incident Bundle delivered", file=sys.stderr)
        return 1

    return collect(arguments.inventory, arguments.since, arguments.output_dir)
