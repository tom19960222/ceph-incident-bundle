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
STATUS_TARGET = "lab-status"
DISCOVER_TARGET = "lab-profile-discover"
ACTIVATE_TARGET = "lab-profile-activate"
PREFLIGHT_TARGET = "lab-preflight"
# The full real-lab gate.  Every earlier step's "you are done for now" message
# points here, so the name lives with the rest of the command vocabulary.
QUALIFICATION_TARGET = "validate-lab"
# Where a passing qualification hands off.  A report carries exactly one next
# action, so even a pass names one ticket rather than a list of possibilities.
CUTOVER_TICKET = "issue #21 (Python cutover qualification in the real lab)"


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


def qualify_command(profile: Path) -> str:
    return (
        f"make {QUALIFICATION_TARGET} LAB_PROFILE={profile} "
        f"{PREFLIGHT_CONFIRMATION_VARIABLE}=1"
    )
