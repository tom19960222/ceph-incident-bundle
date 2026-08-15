"""Thin argument parsing and command dispatch."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import sys
from typing import Sequence

from . import generate_inventory
from .collect import run as collect


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ceph-incident-bundle", allow_abbrev=False
    )
    subcommands = parser.add_subparsers(dest="subcommand", required=True)

    generate_parser = subcommands.add_parser(
        "generate-inventory",
        help="draft a Node Inventory from a hosts file",
        allow_abbrev=False,
    )
    generate_parser.add_argument("--hosts-file", type=Path, default=Path("/etc/hosts"))
    generate_parser.add_argument("--output", type=Path, default=Path("inventory.ini"))
    generate_parser.add_argument("--force", action="store_true")

    collect_parser = subcommands.add_parser(
        "collect",
        help="collect an Incident Bundle from a Node Inventory",
        allow_abbrev=False,
    )
    collect_parser.add_argument("--inventory", type=Path, default=Path("inventory.ini"))
    collect_parser.add_argument("--since", default="24h")
    collect_parser.add_argument("--output-dir", type=Path, default=Path("."))

    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments[:1] != ["collect"]:
        arguments = parser.parse_args(raw_arguments)
    else:
        parser_stderr = StringIO()
        try:
            with redirect_stderr(parser_stderr):
                arguments = parser.parse_args(raw_arguments)
        except SystemExit as error:
            if error.code == 0:
                raise
            print(
                _terminal_safe_parser_diagnostic(parser_stderr.getvalue()),
                end="",
                file=sys.stderr,
            )
            print("FAIL: no Incident Bundle delivered", file=sys.stderr)
            return error.code if isinstance(error.code, int) else 1
    if arguments.subcommand == "generate-inventory":
        return generate_inventory.run(
            arguments.hosts_file, arguments.output, arguments.force
        )

    return collect(arguments.inventory, arguments.since, arguments.output_dir)


def _terminal_safe_parser_diagnostic(value: str) -> str:
    """Escape terminal controls while preserving argparse's line layout."""
    escaped: list[str] = []
    for character in value:
        if character == "\n" or character.isprintable():
            escaped.append(character)
        elif ord(character) <= 0xFF:
            escaped.append(f"\\x{ord(character):02x}")
        else:
            escaped.append(character.encode("unicode_escape").decode("ascii"))
    return "".join(escaped)
