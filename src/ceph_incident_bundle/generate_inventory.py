"""Implementation of the generate-inventory command."""

from __future__ import annotations

from pathlib import Path
import sys

from .inventory import InventoryRejected, draft_inventory


def run(hosts_path: Path, output_path: Path, force: bool) -> int:
    """Draft and write one operator-reviewable Node Inventory."""
    requested_destination = Path(output_path)
    try:
        destination = requested_destination.expanduser().resolve()
    except (OSError, RuntimeError) as error:
        print(
            f"cannot resolve Inventory output {requested_destination}: {error}",
            file=sys.stderr,
        )
        return 1
    output_exists_message = f"Inventory output already exists: {destination}"
    if not force:
        try:
            output_exists = destination.exists()
        except (OSError, RuntimeError) as error:
            print(
                f"cannot inspect Inventory output {destination}: {error}",
                file=sys.stderr,
            )
            return 1
        if output_exists:
            print(output_exists_message, file=sys.stderr)
            return 1

    try:
        generated, problems = draft_inventory(Path(hosts_path))
    except InventoryRejected as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 1

    try:
        mode = "wb" if force else "xb"
        with destination.open(mode) as output:
            output.write(generated)
    except FileExistsError:
        print(output_exists_message, file=sys.stderr)
        return 1
    except OSError as error:
        print(f"cannot write Inventory {destination}: {error}", file=sys.stderr)
        return 1

    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0
