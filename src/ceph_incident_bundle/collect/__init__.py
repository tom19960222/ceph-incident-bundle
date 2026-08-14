"""Readable top-level Incident Bundle collection flow."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

from .. import __version__
from ..inventory import Inventory, InventoryRejected, load_inventory
from .bundle import (
    BundlePublicationError,
    OWNERSHIP_MARKER,
    publish_bundle,
)
from .node import collect_node


_EVIDENCE_WINDOW = re.compile(r"([1-9][0-9]*)([mhdw])\Z", re.ASCII)
# ``collect --since`` is a separate CLI boundary with a deliberately narrower
# grammar and its own diagnostic: unlike Inventory durations it rejects seconds
# and zero, then preserves the accepted spelling in final metadata.
_SECONDS_PER_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def run(inventory_path: Path, since: str, output_directory: Path) -> int:
    """Collect and publish one Target Node Incident Bundle."""
    inventory, since_seconds, output, startup_problems = _validate_startup(
        inventory_path, since, output_directory
    )
    if startup_problems:
        return _nondelivery(startup_problems, status=1)
    assert inventory is not None
    assert since_seconds is not None
    assert output is not None

    started_at = datetime.now(timezone.utc).replace(microsecond=0)
    final_path = output / (
        f"ceph-incident-bundle-{started_at:%Y%m%dT%H%M%SZ}.tar.gz"
    )
    try:
        final_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        return _nondelivery(
            [f"cannot inspect final destination {final_path}: {error}"], status=1
        )
    else:
        return _nondelivery(
            [f"final destination already exists: {final_path}"], status=1
        )

    workspace: Path | None = None
    publication_has_ownership = False
    try:
        workspace = Path(tempfile.mkdtemp(prefix="ceph-incident-work."))
        (workspace / OWNERSHIP_MARKER).write_text(
            str(workspace.resolve()) + "\n", encoding="utf-8"
        )
        private_nodes = workspace / "private" / "nodes"
        contributions = workspace / "admitted" / "node-contributions"
        private_nodes.mkdir(parents=True)
        contributions.mkdir(parents=True)
        (workspace / "admitted" / "kubernetes").mkdir()
        (workspace / "admitted" / "prometheus").mkdir()
        (workspace / "admitted" / "inventory.ini").write_bytes(inventory.snapshot)

        node = inventory.nodes[0]
        problems: list[str] = []
        try:
            problems.extend(
                collect_node(
                    node,
                    ssh_user=inventory.ssh_user,
                    since_seconds=since_seconds,
                    probe_timeout_seconds=inventory.probe_timeout_seconds,
                    ssh_connect_timeout_seconds=inventory.ssh_connect_timeout_seconds,
                    ceph_allowed=inventory.ceph_source == node.inventory_name,
                    staging_directory=private_nodes / node.inventory_name,
                    contribution_directory=contributions / node.inventory_name,
                )
            )
        except Exception as error:
            problems.append(
                f"Target Node {node.inventory_name}: unexpected collection failure: "
                f"{_terminal_safe(str(error))}"
            )
        for problem in problems:
            print(problem, file=sys.stderr)

        publication_has_ownership = True
        cleanup_problem = publish_bundle(
            workspace,
            final_path,
            collector_version=__version__,
            started_at=started_at,
            since=since,
            prior_partial=bool(problems),
        )
        if cleanup_problem is not None:
            problems.append(cleanup_problem)
            print(cleanup_problem, file=sys.stderr)
        outcome = "partial" if problems else "complete"
        print(f"{final_path} ({outcome})")
        return 0
    except KeyboardInterrupt:
        cleanup_problem = None
        if workspace is not None and not publication_has_ownership:
            cleanup_problem = _cleanup_owned_workspace(workspace)
        problems = ["collection interrupted"]
        if cleanup_problem:
            problems.append(cleanup_problem)
        return _nondelivery(problems, status=130)
    except BundlePublicationError as error:
        return _nondelivery(
            [f"cannot publish Incident Bundle: {_terminal_safe(str(error))}"], status=1
        )
    except Exception as error:
        cleanup_problem = None
        if workspace is not None and not publication_has_ownership:
            cleanup_problem = _cleanup_owned_workspace(workspace)
        problems = [f"collection failed: {_terminal_safe(str(error))}"]
        if cleanup_problem:
            problems.append(cleanup_problem)
        return _nondelivery(problems, status=1)


def _validate_startup(
    inventory_path: Path, since: str, output_directory: Path
) -> tuple[Inventory | None, int | None, Path | None, list[str]]:
    problems: list[str] = []
    inventory: Inventory | None = None
    try:
        inventory = load_inventory(Path(inventory_path))
    except InventoryRejected as error:
        problems.extend(error.problems)

    match = _EVIDENCE_WINDOW.fullmatch(since)
    since_seconds: int | None = None
    if match is None:
        problems.append(
            f"invalid evidence window '{since}'; expected a positive integer plus "
            "m, h, d, or w"
        )
    else:
        try:
            since_seconds = int(match.group(1)) * _SECONDS_PER_UNIT[match.group(2)]
        except ValueError:
            # CPython protects decimal conversion from extremely long inputs.
            # That interpreter limit is still a normal CLI validation failure.
            problems.append(
                f"invalid evidence window '{since}'; expected a positive integer "
                "plus m, h, d, or w"
            )

    output: Path | None = None
    try:
        output = Path(output_directory).expanduser().resolve()
        output_mode = output.stat(follow_symlinks=False).st_mode
    except (OSError, RuntimeError) as error:
        problems.append(f"cannot use output directory {output_directory}: {error}")
    else:
        if not stat.S_ISDIR(output_mode):
            problems.append(f"output directory is not an ordinary directory: {output}")

    if inventory is not None and len(inventory.nodes) != 1:
        problems.append(
            "this one-node collection requires exactly one Target Node in the Inventory"
        )
    return inventory, since_seconds, output, problems


def _nondelivery(problems: list[str], *, status: int) -> int:
    for problem in problems:
        print(problem, file=sys.stderr)
    print("FAIL: no Incident Bundle delivered", file=sys.stderr)
    return status


def _cleanup_owned_workspace(workspace: Path) -> str | None:
    """Clean a workspace only while the top-level flow still owns it.

    Publication deliberately proves ownership again after handoff because its cleanup
    decisions and failure reporting belong to the publication capability.
    """
    marker = workspace / OWNERSHIP_MARKER
    try:
        mode = marker.stat(follow_symlinks=False).st_mode
        recorded_path = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        return f"refusing to clean unproven workstation workspace {workspace}: {error}"
    if not stat.S_ISREG(mode) or recorded_path != str(workspace.resolve()):
        return f"refusing to clean unproven workstation workspace {workspace}"
    try:
        shutil.rmtree(workspace)
    except OSError as error:
        return f"cannot remove workstation workspace {workspace}: {error}"
    return None


def _terminal_safe(value: str) -> str:
    """Render top-level exception text without terminal control characters.

    SSH diagnostics perform their own byte decoding and node-prefixed rendering in
    the node capability; the two output boundaries intentionally remain separate.
    """
    escaped: list[str] = []
    for character in value:
        if character.isprintable():
            escaped.append(character)
        elif ord(character) <= 0xFF:
            escaped.append(f"\\x{ord(character):02x}")
        else:
            escaped.append(character.encode("unicode_escape").decode("ascii"))
    return "".join(escaped)
