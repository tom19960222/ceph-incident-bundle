"""Retained real-lab run artifacts: what they cost, and the one way to reclaim them.

A failed `validate-lab` keeps its workdir on purpose.  The read-only safety
contract says a failed gate leaves the scene intact rather than producing a
plausible-looking bundle, so nothing in the collect path may ever delete one —
and nothing here is reachable from a collect.  Reclaiming is an operator
decision taken *after* a failure has been read, never a step the gate takes on
its own.

What that deliberate retention was missing is the other half: nobody was told
what it costs.  Each failed run leaves an evidence tree and two bundles —
gigabytes — under whichever checkout ran it, and they accumulate until the disk
that fills is the one the next collect needs.  This module supplies the two
missing pieces: a read-only inventory `lab-status` reports, and one explicit,
confirmed purge.

Two boundaries are drawn here rather than trusted:

*What survives* is fixed by name, not by size: `report.md`, `report.json` and
each invocation's `collect.log` / `verify.log`.  Those are the audit trail of
what was qualified and are kilobytes; the evidence trees and bundles they
describe are the gigabytes.  The activation ledger lives beside the Lab Profile,
outside any run artifact root, so it is never a candidate at all.

*Where it may write* is the caller's artifact root and nothing else.  Only that
root's own immediate children are considered, only those whose name has the
shape `reserve_run_directory` gives a run, and only real directories — a symlink
in the root is neither followed nor removed.  No path from a profile, a report,
a `LATEST` pointer or any other file can name a removal target.
"""

from __future__ import annotations

import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from validation.lab_commands import clean_command
from validation.lab_profile import safe_display_path
from validation.lab_report import REPORT_JSON_NAME, REPORT_MARKDOWN_NAME


# A run directory is what `reserve_run_directory` creates: a UTC run id, plus
# the `-2`, `-3` … suffix it appends when two runs start in the same second.
RUN_DIRECTORY_PATTERN = re.compile(r"\A\d{8}T\d{6}Z(-\d+)?\Z")
RUN_ID_LENGTH = len("YYYYmmddTHHMMSSZ")

# Kept at the run directory's top level: the persisted verdict, in both formats.
REPORT_NAMES = (REPORT_JSON_NAME, REPORT_MARKDOWN_NAME)
# Kept one level down, inside each implementation's output directory: the
# command ledgers `validation/lab_qualify.py` writes beside each bundle.  They
# are the diagnostic trail a purged run is read through afterwards.
LEDGER_NAMES = ("collect.log", "verify.log")

DEFAULT_KEEP = 1

STATUS_PURGED = "artifacts-purged"
STATUS_NOT_CONFIRMED = "purge-not-confirmed"
STATUS_NOTHING_TO_PURGE = "nothing-to-purge"
STATUS_INCOMPLETE = "purge-incomplete"


@dataclass(frozen=True)
class RunArtifacts:
    """One run directory's reclaimable payload — never its report or ledgers."""

    directory: Path
    run_id: str
    size: int
    targets: tuple[Path, ...] = ()

    @property
    def timestamp(self) -> str | None:
        """The run id read back as a plain UTC timestamp, when it is one."""

        head = self.run_id[:RUN_ID_LENGTH]
        if not re.fullmatch(r"\d{8}T\d{6}Z", head):
            return None
        return (
            f"{head[0:4]}-{head[4:6]}-{head[6:8]}T"
            f"{head[9:11]}:{head[11:13]}:{head[13:15]}Z"
        )

    def document(self) -> dict[str, object]:
        return {
            "run": self.run_id,
            "directory": safe_display_path(self.directory),
            "bytes": self.size,
            "size": human_size(self.size),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class ArtifactInventory:
    """Every retained run artifact under one root, oldest run first."""

    root: Path = field(default_factory=Path)
    runs: tuple[RunArtifacts, ...] = ()

    @property
    def count(self) -> int:
        return len(self.runs)

    @property
    def size(self) -> int:
        return sum(run.size for run in self.runs)

    @property
    def oldest(self) -> RunArtifacts | None:
        return self.runs[0] if self.runs else None

    def summary(self) -> dict[str, object]:
        oldest = self.oldest
        return {
            "root": safe_display_path(self.root),
            "runs": self.count,
            "bytes": self.size,
            "size": human_size(self.size),
            "oldest_run": oldest.run_id if oldest else None,
            "oldest_timestamp": oldest.timestamp if oldest else None,
        }

    def line(self) -> str:
        """The one status line describing what has piled up, if anything has."""

        if not self.runs:
            return "none retained"
        oldest = self.runs[0]
        return (
            f"{self.count} run(s), {human_size(self.size)}, oldest "
            f"{oldest.run_id} — reclaim with {clean_command()}"
        )


@dataclass(frozen=True)
class PurgeResult:
    """What a purge removed, or would remove, and the single next step."""

    root: Path
    keep: int
    kept: tuple[RunArtifacts, ...]
    purged: tuple[RunArtifacts, ...]
    status: str
    next_action: str
    confirmed: bool = False
    reclaimed: int = 0
    failures: tuple[str, ...] = ()
    blocked_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_PURGED, STATUS_NOTHING_TO_PURGE)

    @property
    def reclaimable(self) -> int:
        return sum(run.size for run in self.purged)

    def summary(self) -> dict[str, object]:
        return {
            "root": safe_display_path(self.root),
            "keep": self.keep,
            "confirmed": self.confirmed,
            "kept": [run.document() for run in self.kept],
            "purged": [run.document() for run in self.purged],
            "reclaimable_bytes": self.reclaimable,
            "reclaimed_bytes": self.reclaimed,
            "reclaimed": human_size(self.reclaimed),
            "failures": list(self.failures),
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "next_action": self.next_action,
        }

    def text(self) -> str:
        def row(label: str, value: str) -> str:
            return f"  {label + ':':<12}{value}"

        lines = [
            "Lab run artifact cleanup",
            row("root", safe_display_path(self.root)),
            row("keeping", f"the {self.keep} most recent run(s) with artifacts"),
        ]
        for run in self.kept:
            lines.append(row("kept", f"{run.run_id} ({human_size(run.size)})"))
        for run in self.purged:
            lines.append(
                row(
                    "purged" if self.confirmed else "to purge",
                    f"{run.run_id} ({human_size(run.size)})",
                )
            )
        if not self.kept and not self.purged:
            lines.append(row("artifacts", "none retained"))
        if self.confirmed:
            lines.append(row("reclaimed", human_size(self.reclaimed)))
        else:
            lines.append(row("reclaims", human_size(self.reclaimable)))
        for failure in self.failures:
            lines.append(row("failure", failure))
        lines.append(row("status", self.status))
        if self.blocked_reason:
            lines.append(row("blocked by", self.blocked_reason))
        lines += ["", f"next action: {self.next_action}"]
        return "\n".join(lines) + "\n"


def human_size(size: int) -> str:
    """Render a byte count in binary units, so a reported size matches `du`."""

    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def scan_artifacts(root: Path) -> ArtifactInventory:
    """Inventory the reclaimable payload under one artifact root, reading only.

    A run whose reports and ledgers are all that is left is not reported: it
    costs kilobytes and there is nothing to reclaim from it.  An unreadable
    directory is skipped rather than raised — `lab-status` must still answer.
    """

    runs: list[RunArtifacts] = []
    for directory in _run_directories(root):
        targets = _removal_targets(directory)
        if not targets:
            continue
        runs.append(
            RunArtifacts(
                directory=directory,
                run_id=directory.name,
                size=sum(_size(target) for target in targets),
                targets=tuple(targets),
            )
        )
    return ArtifactInventory(root=root, runs=tuple(runs))


def purge_artifacts(
    root: Path, *, confirmed: bool, keep: int = DEFAULT_KEEP
) -> PurgeResult:
    """Remove the reclaimable payload of every run but the most recent `keep`.

    Without `confirmed` this removes nothing and describes what it would remove:
    a plain `make lab-clean` is a preview, and deleting evidence takes the same
    kind of explicit opt-in as activating a profile.
    """

    if keep < 0:
        raise ValueError("keep must not be negative")
    inventory = scan_artifacts(root)
    # The newest `keep` runs *that still have artifacts* are the ones kept: a
    # report-only directory left by a preflight protects nothing, and shielding
    # it would purge the failed run the operator is most likely still reading.
    boundary = inventory.count - keep
    purged = inventory.runs[: max(boundary, 0)]
    kept = inventory.runs[max(boundary, 0) :]

    if not purged:
        return PurgeResult(
            root=root,
            keep=keep,
            kept=kept,
            purged=(),
            confirmed=confirmed,
            status=STATUS_NOTHING_TO_PURGE,
            next_action=(
                f"Nothing to reclaim under {root} — continue with the lab "
                "workflow's own next action"
            ),
        )
    if not confirmed:
        return PurgeResult(
            root=root,
            keep=keep,
            kept=kept,
            purged=purged,
            confirmed=False,
            status=STATUS_NOT_CONFIRMED,
            blocked_reason="the purge was not explicitly confirmed",
            next_action=(
                "Re-run "
                + clean_command(root=root, keep=keep, confirmed=True)
                + " once the listed run(s) have been read — it deletes "
                + human_size(sum(run.size for run in purged))
                + " of evidence and keeps every report"
            ),
        )

    reclaimed = 0
    failures: list[str] = []
    removed: list[RunArtifacts] = []
    for run in purged:
        run_failures: list[str] = []
        for target in run.targets:
            size = _size(target)
            try:
                _remove(target)
            except OSError as error:
                run_failures.append(f"{run.run_id}: cannot remove {target.name} ({error})")
                continue
            reclaimed += size
        _prune_empty_directories(run.directory)
        failures.extend(run_failures)
        if not run_failures:
            removed.append(run)
    if failures:
        return PurgeResult(
            root=root,
            keep=keep,
            kept=kept,
            purged=purged,
            confirmed=True,
            reclaimed=reclaimed,
            failures=tuple(failures),
            status=STATUS_INCOMPLETE,
            blocked_reason=f"{len(failures)} artifact(s) could not be removed",
            next_action=(
                f"Remove the listed leftovers under {root} by hand, then re-run "
                + clean_command(root=root, keep=keep, confirmed=True)
            ),
        )
    return PurgeResult(
        root=root,
        keep=keep,
        kept=kept,
        purged=tuple(removed),
        confirmed=True,
        reclaimed=reclaimed,
        status=STATUS_PURGED,
        next_action=(
            f"Reclaimed {human_size(reclaimed)}; every report was kept — continue "
            "with the lab workflow's own next action"
        ),
    )


def _run_directories(root: Path) -> list[Path]:
    """The root's own run directories, oldest first — nothing else, ever.

    Run ids are fixed-width UTC stamps, so sorting them by name sorts them by
    time.  A name that is not one, a file, or a symlink to somewhere else is not
    a run directory and is never looked inside.
    """

    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    return [
        child
        for child in children
        if RUN_DIRECTORY_PATTERN.match(child.name) and _is_directory(child)
    ]


def _removal_targets(directory: Path) -> list[Path]:
    """Everything in one run directory that is not a report or a command ledger."""

    targets: list[Path] = []
    for child in _children(directory):
        if child.name in REPORT_NAMES and _is_regular_file(child):
            continue
        if _is_directory(child):
            targets.extend(
                grandchild
                for grandchild in _children(child)
                if not (grandchild.name in LEDGER_NAMES and _is_regular_file(grandchild))
            )
            continue
        targets.append(child)
    return targets


def _children(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except OSError:
        return []


def _mode(path: Path) -> int:
    try:
        return path.lstat().st_mode
    except OSError:
        return 0


def _is_directory(path: Path) -> bool:
    """A real directory — a symlink to one is not, and is never descended into."""

    return stat.S_ISDIR(_mode(path))


def _is_regular_file(path: Path) -> bool:
    """A real file: a symlink named `report.json` is not the report it names."""

    return stat.S_ISREG(_mode(path))


def _size(path: Path) -> int:
    """The bytes one removal target occupies, never following a symlink out."""

    try:
        info = path.lstat()
    except OSError:
        return 0
    if not stat.S_ISDIR(info.st_mode):
        return info.st_size
    total = 0
    for child in _children(path):
        total += _size(child)
    return total


def _remove(path: Path) -> None:
    """Remove one target: a link is unlinked, never resolved and followed."""

    if _is_directory(path):
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)


def _prune_empty_directories(run_directory: Path) -> None:
    """Drop an implementation directory a purge emptied; keep the run itself.

    The run directory stays even when nothing is left in it but its reports —
    it is the address every report, `LATEST` pointer and handoff already names.
    """

    for child in _children(run_directory):
        if not _is_directory(child):
            continue
        if _children(child):
            continue
        try:
            child.rmdir()
        except OSError:
            pass
