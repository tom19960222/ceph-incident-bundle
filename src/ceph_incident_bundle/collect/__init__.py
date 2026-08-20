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
from .kubernetes import collect_kubernetes
from .node import collect_node
from .prometheus import collect_prometheus


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
    if inventory is None or since_seconds is None or output is None:
        # ``_validate_startup`` guarantees these once it reports no problems.
        # This stays an explicit raise (rather than ``assert``) so it still
        # fails loudly under ``python -O``, where ``assert`` is stripped and
        # the ``None`` case would otherwise proceed silently.
        raise RuntimeError(
            "internal error: startup validation reported success without "
            "an Inventory, evidence window, or output directory"
        )

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
        (workspace / "admitted" / "inventory.ini").write_bytes(inventory.snapshot)

        problems: list[str] = []
        for node in inventory.nodes:
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

        kubernetes_contribution = workspace / "admitted" / "kubernetes"
        if inventory.kubernetes_context is not None:
            try:
                problems.extend(
                    collect_kubernetes(
                        context=inventory.kubernetes_context,
                        consumer_namespace=inventory.consumer_namespace,
                        operator_namespace=inventory.operator_namespace,
                        since=since,
                        probe_timeout_seconds=inventory.probe_timeout_seconds,
                        staging_directory=workspace / "private" / "kubernetes",
                        contribution_directory=kubernetes_contribution,
                    )
                )
            except Exception as error:
                problems.append(
                    "Kubernetes: unexpected collection failure: "
                    f"{_terminal_safe(str(error))}"
                )
        if not kubernetes_contribution.exists():
            kubernetes_contribution.mkdir()
        prometheus_contribution = workspace / "admitted" / "prometheus"
        if inventory.prometheus_url is not None:
            try:
                problems.extend(
                    collect_prometheus(
                        url=inventory.prometheus_url,
                        since_seconds=since_seconds,
                        request_timeout_seconds=inventory.request_timeout_seconds,
                        metrics_filter_regex=inventory.metrics_filter_regex,
                        query_step=inventory.query_step,
                        staging_directory=workspace / "private" / "prometheus",
                        contribution_directory=prometheus_contribution,
                    )
                )
            except Exception as error:
                problems.append(
                    "Prometheus: unexpected collection failure: "
                    f"{_terminal_safe(str(error))}"
                )
        if not prometheus_contribution.exists():
            prometheus_contribution.mkdir()

        try:
            for problem in problems:
                print(_terminal_safe(problem), file=sys.stderr)
        except Exception:
            # Returned problems still determine the bundle outcome when the
            # diagnostic stream itself is no longer writable.
            pass

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
            try:
                print(_terminal_safe(cleanup_problem), file=sys.stderr)
            except Exception:
                # Returned problems still determine the bundle outcome when the
                # diagnostic stream itself is no longer writable.
                pass
        outcome = "partial" if problems else "complete"
        try:
            print(f"{final_path} ({outcome})")
        except Exception as error:
            # Publication has already made the final path visible.  Losing the
            # result stream is a delivery warning, never bundle nondelivery.
            print(
                f"Incident Bundle delivered at {final_path} ({outcome}), but cannot "
                "write the final standard-output result: "
                f"{_terminal_safe(str(error))}",
                file=sys.stderr,
            )
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
            normalized_seconds = (
                int(match.group(1)) * _SECONDS_PER_UNIT[match.group(2)]
            )
            # SSH receives this CLI control as canonical decimal seconds.  Form
            # that representation during startup so it cannot fail after work begins.
            str(normalized_seconds)
        except ValueError:
            problems.append(
                f"invalid evidence window '{since}'; expected a positive integer "
                "plus m, h, d, or w"
            )
        else:
            since_seconds = normalized_seconds

    output: Path | None = None
    try:
        output = Path(output_directory).expanduser().resolve()
        output_mode = output.stat(follow_symlinks=False).st_mode
    except (OSError, RuntimeError) as error:
        problems.append(f"cannot use output directory {output_directory}: {error}")
    else:
        if not stat.S_ISDIR(output_mode):
            problems.append(f"output directory is not an ordinary directory: {output}")

    return inventory, since_seconds, output, problems


def _nondelivery(problems: list[str], *, status: int) -> int:
    for problem in problems:
        print(_terminal_safe(problem), file=sys.stderr)
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
