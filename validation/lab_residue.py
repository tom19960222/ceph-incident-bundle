"""Remote residue: proof that the live Python collect left nothing on a node.

The collector does node work inside a temporary workspace under the node's
`TMPDIR` and must remove it, so anything of that shape newly present after the
live run is a leak of collector state onto a machine we only had permission to
read.

Ownership is established by *comparison*, not by deletion.  One listing is taken
before the live collect and one after it; only what appeared in between
is attributed to this run, and a workspace whose name carries one of the run's
invocation identifiers is named as such.  Nothing here removes a workspace or
signals a process: a residue check that can clean up is a residue check that can
be made to pass, and the runbook requires the leftovers to reach a human instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from validation.lab_probe import LabProber
from validation.lab_profile import LabHost


RESULT_CLEAN = "clean"
RESULT_RESIDUE = "residue"
RESULT_UNREADABLE = "unreadable"
# A node with a genuinely broken TMPDIR could list a great many entries; the
# verdict only needs enough of them to start an investigation.
REPORTED_ENTRY_LIMIT = 20
ENTRY_DISPLAY_LIMIT = 200


class ResidueUnavailable(Exception):
    """A node's residue listing could not be read, so nothing can be proven."""


@dataclass(frozen=True)
class ResidueListing:
    """One node's collector leftovers at one moment."""

    host: str
    workspaces: tuple[str, ...]
    processes: tuple[str, ...]


def observe_residue(
    prober: LabProber, host: LabHost, known_hosts: Path
) -> ResidueListing:
    """List one node's collector workspaces and helper processes."""

    outcome = prober.list_collector_residue(host, known_hosts)
    if not outcome.ok or outcome.value is None:
        raise ResidueUnavailable(f"{host.name}: {outcome.detail}")
    workspaces: list[str] = []
    processes: list[str] = []
    for line in outcome.value.splitlines():
        kind, separator, value = line.partition("\t")
        if not separator or not value.strip():
            continue
        if kind == "workspace":
            workspaces.append(value.strip())
        elif kind == "process":
            processes.append(value.strip()[:ENTRY_DISPLAY_LIMIT])
    return ResidueListing(host.name, tuple(sorted(set(workspaces))), tuple(sorted(set(processes))))


def compare_residue(
    baseline: ResidueListing,
    after: ResidueListing,
    *,
    invocation_ids: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Decide one node's residue verdict and describe it in one line.

    Anything already present before the live collect is reported as pre-existing
    rather than silently ignored: it is not this run's leak, but an operator
    reading the report should still know the node was not clean to begin with.
    """

    new_workspaces = [
        entry for entry in after.workspaces if entry not in baseline.workspaces
    ]
    new_processes = [
        entry for entry in after.processes if entry not in baseline.processes
    ]
    pre_existing = len(baseline.workspaces) + len(baseline.processes)
    if not new_workspaces and not new_processes:
        detail = "no collector workspace or helper process left by either invocation"
        if pre_existing:
            detail += f"; {pre_existing} pre-existing entry(s) predate this run"
        return RESULT_CLEAN, detail
    parts: list[str] = []
    if new_workspaces:
        parts.append(
            "workspace(s) "
            + ", ".join(
                _attributed(entry, invocation_ids)
                for entry in new_workspaces[:REPORTED_ENTRY_LIMIT]
            )
            + _truncation(new_workspaces)
        )
    if new_processes:
        parts.append(
            "helper process(es) "
            + ", ".join(new_processes[:REPORTED_ENTRY_LIMIT])
            + _truncation(new_processes)
        )
    return RESULT_RESIDUE, "; ".join(parts)


def _attributed(entry: str, invocation_ids: tuple[str, ...]) -> str:
    for invocation_id in invocation_ids:
        if invocation_id and invocation_id in entry:
            return f"{entry} (invocation {invocation_id})"
    return entry


def _truncation(entries: list[str]) -> str:
    hidden = len(entries) - REPORTED_ENTRY_LIMIT
    return f" and {hidden} more" if hidden > 0 else ""
