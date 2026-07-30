"""Explicit, audited activation of a reviewed Lab Profile Candidate.

Activation is the one step that turns untrusted discovery output into the profile
the real-lab gate will trust, so it is deliberately hard to do by accident:
it needs an explicit confirmation, it refuses to promote anything that is not a
reviewed candidate, and replacing an existing active profile needs a second
opt-in.  Every activation appends one record to a local-only ledger beside the
active profile, so a later agent can see which identity was accepted and when.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from validation.lab_local import append_owner_only, utc_timestamp, write_owner_only
from validation.lab_profile import LabProfile, LabProfileError, load_profile, safe_display_path


ACTIVATION_LOG_NAME = "lab-activation.log.jsonl"
CONFIRMATION_VARIABLE = "CEPH_INCIDENT_LAB_ACTIVATE"
STATUS_COMPLETE = "activation-complete"
STATUS_BLOCKED = "activation-blocked"


@dataclass(frozen=True)
class ActivationResult:
    """What activation did, or refused to do, and the single next step."""

    candidate_path: Path
    active_path: Path
    status: str
    next_action: str
    activated: bool = False
    active_hash: str | None = None
    previous_hash: str | None = None
    replaced: bool = False
    log_path: Path | None = None
    blocked_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETE

    def summary(self) -> dict[str, object]:
        return {
            "candidate_profile": safe_display_path(self.candidate_path),
            "active_profile": safe_display_path(self.active_path),
            "activated": self.activated,
            "active_profile_hash": self.active_hash,
            "previous_profile_hash": self.previous_hash,
            "replaced_active_profile": self.replaced,
            "activation_log": safe_display_path(self.log_path) if self.log_path else None,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "next_action": self.next_action,
        }


def activate(
    candidate_path: Path,
    active_path: Path,
    *,
    confirmed: bool,
    replace_active: bool = False,
) -> ActivationResult:
    """Promote one reviewed candidate to the active Lab Profile."""

    def blocked(reason: str, action: str) -> ActivationResult:
        return ActivationResult(
            candidate_path=candidate_path,
            active_path=active_path,
            status=STATUS_BLOCKED,
            blocked_reason=reason,
            next_action=action,
        )

    if not confirmed:
        return blocked(
            "activation was not explicitly confirmed",
            "Re-run make lab-profile-activate with "
            f"{CONFIRMATION_VARIABLE}=1 once the candidate identity has been reviewed",
        )
    if candidate_path == active_path:
        return blocked(
            "the candidate and the active profile are the same file",
            "Re-run make lab-profile-activate with LAB_PROFILE and LAB_CANDIDATE "
            "pointing at different files",
        )
    candidate = load_profile(candidate_path)
    if candidate.state != "candidate":
        return blocked(
            f"{safe_display_path(candidate_path)} is a {candidate.state} profile, "
            "not a reviewed Lab Profile Candidate",
            f"Run make lab-profile-discover LAB_PROFILE={candidate_path} to produce a "
            "Lab Profile Candidate, review it, then activate that file",
        )

    previous: LabProfile | None = None
    if active_path.exists():
        try:
            previous = load_profile(active_path)
        except LabProfileError as error:
            if not replace_active:
                return blocked(
                    f"{safe_display_path(active_path)} exists but is not a readable "
                    f"Lab Profile ({error})",
                    "Re-run make lab-profile-activate with --replace-active to replace "
                    f"{safe_display_path(active_path)}, or move that file aside first",
                )
        if previous is not None and previous.state == "active" and not replace_active:
            return blocked(
                f"{safe_display_path(active_path)} is already an active Lab Profile",
                "Compare the candidate against the active profile and re-run "
                "make lab-profile-activate with --replace-active to accept the new "
                "identity",
            )

    activated = candidate.with_state("active", path=active_path)
    write_owner_only(active_path, _active_document(activated, candidate_path, candidate))
    log_path = active_path.parent / ACTIVATION_LOG_NAME
    previous_hash = previous.profile_hash if previous is not None else None
    append_owner_only(
        log_path,
        json.dumps(
            _audit_record(activated, candidate, candidate_path, previous_hash),
            sort_keys=True,
        ),
    )
    return ActivationResult(
        candidate_path=candidate_path,
        active_path=active_path,
        status=STATUS_COMPLETE,
        activated=True,
        active_hash=activated.profile_hash,
        previous_hash=previous_hash,
        replaced=previous is not None,
        log_path=log_path,
        next_action=(
            f"Run make lab-status LAB_PROFILE={active_path} to confirm the activated "
            "profile and read the next step"
        ),
    )


def _audit_record(
    activated: LabProfile,
    candidate: LabProfile,
    candidate_path: Path,
    previous_hash: str | None,
) -> dict[str, object]:
    """The activation ledger record: accepted identity only, never credentials."""

    return {
        "activated_at": utc_timestamp(),
        "profile_name": activated.name,
        "active_profile": safe_display_path(activated.path),
        "active_profile_hash": activated.profile_hash,
        "candidate_profile": safe_display_path(candidate_path),
        "candidate_profile_hash": candidate.profile_hash,
        "previous_profile_hash": previous_hash,
        "ceph_fsid": activated.ceph_fsid,
        "rook_fsid": activated.rook_fsid,
        "prometheus_url": activated.prometheus_url,
        "hosts": [
            {
                "name": host.name,
                "address": host.address,
                "hostname": host.hostname,
                "ssh_fingerprints": list(host.ssh_fingerprints),
            }
            for host in activated.hosts
        ],
    }


def _active_document(
    activated: LabProfile, candidate_path: Path, candidate: LabProfile
) -> str:
    return (
        "# Active Lab Profile — reviewed and explicitly activated.\n"
        f"# Activated by make lab-profile-activate at {utc_timestamp()}\n"
        f"# Source candidate: {candidate_path} ({candidate.profile_hash})\n"
        "# This file is local-only; never commit it.\n"
        "\n" + activated.render()
    )
