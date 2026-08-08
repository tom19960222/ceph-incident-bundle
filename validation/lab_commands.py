"""The operator-facing command vocabulary every `next_action` is built from.

A `next_action` is only useful if it can be pasted into a shell, so the target
names and the two confirmation variables live here once instead of being spelled
out at each site that produces an action.  Renaming a target or an opt-in
variable is then one edit, not a hunt through four modules.

Actions quote real absolute paths, not the `safe_display_path` form used
elsewhere: a shortened path would make the printed command wrong.  Nothing here
takes a value from the lab, so no action can carry lab output into a shell.
"""

from __future__ import annotations

from pathlib import Path


ACTIVATION_CONFIRMATION_VARIABLE = "CEPH_INCIDENT_LAB_ACTIVATE"
PREFLIGHT_CONFIRMATION_VARIABLE = "CEPH_INCIDENT_LAB_CONFIRM"
# Reclaiming a failed run's retained evidence is destructive and unrecoverable,
# so it gets its own opt-in rather than sharing one with a command that reads.
CLEAN_CONFIRMATION_VARIABLE = "CEPH_INCIDENT_LAB_CLEAN"
STATUS_TARGET = "lab-status"
DISCOVER_TARGET = "lab-profile-discover"
ACTIVATE_TARGET = "lab-profile-activate"
PREFLIGHT_TARGET = "lab-preflight"
CLEAN_TARGET = "lab-clean"
# The full real-lab gate.  Every earlier step's "you are done for now" message
# points here, so the name lives with the rest of the command vocabulary.
QUALIFICATION_TARGET = "validate-lab"
# Where a passing qualification hands off.  A report carries exactly one next
# action, so even a pass names one ticket rather than a list of possibilities.
CUTOVER_TICKET = "issue #22 (record the post-cutover PASS proof and close the cutover)"


def status_command(profile: Path) -> str:
    return f"make {STATUS_TARGET} LAB_PROFILE={profile}"


def discover_command(profile: Path, *, replace_candidate: bool = False) -> str:
    command = f"make {DISCOVER_TARGET} LAB_PROFILE={profile}"
    if replace_candidate:
        command += " LAB_ARGS=--replace-candidate"
    return command


def activate_command(
    profile: Path | str, candidate: Path, *, replace_active: bool = False
) -> str:
    command = (
        f"make {ACTIVATE_TARGET} LAB_PROFILE={profile} LAB_CANDIDATE={candidate} "
        f"{ACTIVATION_CONFIRMATION_VARIABLE}=1"
    )
    if replace_active:
        command += " LAB_ARGS=--replace-active"
    return command


def preflight_command(profile: Path) -> str:
    return (
        f"make {PREFLIGHT_TARGET} LAB_PROFILE={profile} "
        f"{PREFLIGHT_CONFIRMATION_VARIABLE}=1"
    )


def qualify_command(profile: Path, baseline_report: Path | None = None) -> str:
    baseline = (
        str(baseline_report)
        if baseline_report is not None
        else "/absolute/path/to/report.json"
    )
    return (
        f"make {QUALIFICATION_TARGET} LAB_PROFILE={profile} "
        f"LAB_BASELINE_REPORT={baseline} {PREFLIGHT_CONFIRMATION_VARIABLE}=1"
    )


def clean_command(
    *, root: Path | None = None, keep: int | None = None, confirmed: bool = False
) -> str:
    """The retained-artifact cleanup command, with the arguments it was given.

    No Lab Profile appears here, and that is the point: cleanup writes only
    inside the artifact root it is handed, so nothing about a profile — least of
    all a path read out of one — takes part in deciding what gets deleted.
    """

    arguments = []
    if root is not None:
        arguments.append(f"--runs-dir {root}")
    if keep is not None:
        arguments.append(f"--keep {keep}")
    command = f"make {CLEAN_TARGET}"
    if arguments:
        command += " LAB_ARGS='" + " ".join(arguments) + "'"
    if confirmed:
        command += f" {CLEAN_CONFIRMATION_VARIABLE}=1"
    return command
