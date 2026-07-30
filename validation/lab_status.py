"""`lab-status`: the fresh-agent entry point, and the only one that never connects.

An agent taking over must not need a previous chat.  This module reads local
state only — the profile, the credential paths' existence and permissions, any
pending candidate, the activation ledger and the latest Lab Validation Report —
and reports where the workflow is, what is blocking it, and exactly one next
step.  It runs no external command, reaches no lab and rewrites nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from validation.lab_activation import ACTIVATION_LOG_NAME
from validation.lab_discovery import CANDIDATE_SUFFIX
from validation.lab_preflight import credential_problem
from validation.lab_profile import LabProfile, LabProfileError, load_profile, safe_display_path
from validation.lab_report import latest_report


STATE_PROFILE_MISSING = "profile-missing"
STATE_PROFILE_INVALID = "profile-invalid"
STATE_PROFILE_BOOTSTRAP = "profile-bootstrap"
STATE_PROFILE_CANDIDATE = "profile-candidate"
STATE_CREDENTIAL_PATH_INVALID = "credential-path-invalid"
STATE_CANDIDATE_PENDING_REVIEW = "candidate-pending-review"
STATE_READY_FOR_PREFLIGHT = "ready-for-preflight"
STATE_PREFLIGHT_PASSED = "preflight-passed"
STATE_LAST_ATTEMPT_FAILED = "last-attempt-failed"


@dataclass(frozen=True)
class CredentialPathStatus:
    """One credential path's local usability — never its content."""

    label: str
    display: str
    usable: bool
    problem: str | None


@dataclass(frozen=True)
class CandidateStatus:
    """One Lab Profile Candidate sitting beside the profile."""

    path: Path
    state: str | None
    profile_hash: str | None
    problem: str | None
    matches_active: bool = False

    @property
    def display(self) -> str:
        return str(self.path)

    @property
    def pending_review(self) -> bool:
        return self.state == "candidate" and not self.matches_active


@dataclass(frozen=True)
class LabStatus:
    """Where the lab workflow is, and the single next step."""

    profile_path: Path
    state: str
    next_action: str
    profile: LabProfile | None = None
    profile_error: str | None = None
    credential_paths: tuple[CredentialPathStatus, ...] = ()
    candidates: tuple[CandidateStatus, ...] = ()
    last_activation: dict[str, object] | None = None
    latest_report: dict[str, object] | None = None
    runs_directory: Path = field(default_factory=Path)
    blocked_reason: str | None = None

    @property
    def ready(self) -> bool:
        return self.state in (STATE_READY_FOR_PREFLIGHT, STATE_PREFLIGHT_PASSED)

    def summary(self) -> dict[str, object]:
        return {
            "profile": safe_display_path(self.profile_path),
            "profile_present": self.profile is not None,
            "profile_state": self.profile.state if self.profile else None,
            "profile_hash": self.profile.profile_hash if self.profile else None,
            "profile_name": self.profile.name if self.profile else None,
            "profile_error": self.profile_error,
            "identity_complete": bool(self.profile and self.profile.identity_complete),
            "missing_identity": list(self.profile.missing_identity()) if self.profile else [],
            "hosts": [host.name for host in self.profile.hosts] if self.profile else [],
            "credential_paths": [
                {
                    "label": path.label,
                    "path": path.display,
                    "usable": path.usable,
                    "problem": path.problem,
                }
                for path in self.credential_paths
            ],
            "candidates": [
                {
                    "path": candidate.display,
                    "state": candidate.state,
                    "hash": candidate.profile_hash,
                    "problem": candidate.problem,
                    "pending_review": candidate.pending_review,
                }
                for candidate in self.candidates
            ],
            "last_activation": self.last_activation,
            "runs_directory": safe_display_path(self.runs_directory),
            "latest_report": self.latest_report,
            "state": self.state,
            "blocked_reason": self.blocked_reason,
            "next_action": self.next_action,
        }

    def text(self) -> str:
        lines = [
            "Lab status",
            f"  profile:        {safe_display_path(self.profile_path)}",
        ]
        if self.profile is not None:
            lines += [
                f"  profile state:  {self.profile.state}",
                f"  profile hash:   {self.profile.profile_hash}",
                f"  profile name:   {self.profile.name}",
                f"  hosts:          {', '.join(host.name for host in self.profile.hosts)}",
                f"  ceph seed:      {self.profile.ceph_seed}",
                f"  prometheus:     {self.profile.prometheus_url}",
            ]
            missing = self.profile.missing_identity()
            if missing:
                lines.append(f"  missing identity: {', '.join(missing)}")
        else:
            lines.append(f"  profile error:  {self.profile_error}")
        for path in self.credential_paths:
            verdict = "ok" if path.usable else f"UNUSABLE ({path.problem})"
            lines.append(f"  {path.label}: {path.display} — {verdict}")
        if self.candidates:
            for candidate in self.candidates:
                if candidate.problem:
                    detail = f"unreadable ({candidate.problem})"
                elif candidate.matches_active:
                    detail = "matches the active profile"
                else:
                    detail = f"{candidate.state}, awaiting review"
                lines.append(f"  candidate:      {candidate.display} — {detail}")
        else:
            lines.append("  candidate:      none")
        if self.last_activation is not None:
            lines.append(
                f"  last activation: {self.last_activation.get('activated_at')} "
                f"({self.last_activation.get('active_profile_hash')})"
            )
        if self.latest_report is not None:
            lines.append(
                f"  latest report:  {self.latest_report.get('directory')} "
                f"[{self.latest_report.get('status')}]"
            )
        else:
            lines.append(f"  latest report:  none in {safe_display_path(self.runs_directory)}")
        lines += [
            f"  state:          {self.state}",
        ]
        if self.blocked_reason:
            lines.append(f"  blocked by:     {self.blocked_reason}")
        lines += ["", f"next action: {self.next_action}"]
        return "\n".join(lines) + "\n"


def lab_status(profile_path: Path, *, runs_directory: Path) -> LabStatus:
    """Read local lab state and decide the single next step."""

    candidates = _candidates(profile_path)
    report = latest_report(runs_directory)
    activation = _last_activation(profile_path)
    try:
        profile = load_profile(profile_path)
    except LabProfileError as error:
        missing = not profile_path.exists()
        return LabStatus(
            profile_path=profile_path,
            state=STATE_PROFILE_MISSING if missing else STATE_PROFILE_INVALID,
            profile_error=str(error),
            blocked_reason=str(error),
            next_action=_missing_profile_action(profile_path)
            if missing
            else (
                f"Fix {safe_display_path(profile_path)} ({error}), then re-run "
                f"make lab-status LAB_PROFILE={profile_path}"
            ),
            candidates=candidates,
            last_activation=activation,
            latest_report=report,
            runs_directory=runs_directory,
        )

    credential_paths = _credential_paths(profile)
    candidates = _annotate_candidates(candidates, profile)
    state, blocked, action = _verdict(profile, credential_paths, candidates, report)
    return LabStatus(
        profile_path=profile_path,
        state=state,
        profile=profile,
        credential_paths=credential_paths,
        blocked_reason=blocked,
        next_action=action,
        candidates=candidates,
        last_activation=activation,
        latest_report=report,
        runs_directory=runs_directory,
    )


def _verdict(
    profile: LabProfile,
    credential_paths: tuple[CredentialPathStatus, ...],
    candidates: tuple[CandidateStatus, ...],
    report: dict[str, object] | None,
) -> tuple[str, str | None, str]:
    """Decide the one next step, most blocking condition first.

    The order matters more than any single branch.  Credential paths come first
    because every later step needs them; a candidate awaiting review comes next,
    ahead of the profile's own state, so a workflow that already produced a
    candidate is told to review it instead of being sent back to discovery in a
    loop.
    """

    path = profile.path
    unusable = [entry for entry in credential_paths if not entry.usable]
    if unusable:
        first = unusable[0]
        return (
            STATE_CREDENTIAL_PATH_INVALID,
            f"{first.label} {first.display} {first.problem}",
            f"Fix {first.label} in {path} — {first.display} {first.problem} — then "
            f"re-run make lab-status LAB_PROFILE={path}",
        )
    pending = [candidate for candidate in candidates if candidate.pending_review]
    if pending:
        first = pending[0]
        replace = " --replace-active" if profile.state == "active" else ""
        compared = "the active profile" if profile.state == "active" else "the expected topology"
        return (
            STATE_CANDIDATE_PENDING_REVIEW,
            f"{first.display} is a Lab Profile Candidate awaiting review",
            f"Review {first.display} against {compared} and either run "
            f"make lab-profile-activate LAB_PROFILE={path} "
            f"LAB_CANDIDATE={first.display} CEPH_INCIDENT_LAB_ACTIVATE=1{replace}"
            ", or remove that candidate",
        )
    if profile.state == "bootstrap":
        return (
            STATE_PROFILE_BOOTSTRAP,
            "the profile records no reviewed lab identity",
            f"Run make lab-profile-discover LAB_PROFILE={path} to produce a Lab "
            "Profile Candidate",
        )
    if profile.state == "candidate":
        return (
            STATE_PROFILE_CANDIDATE,
            "the profile is an unreviewed Lab Profile Candidate",
            f"Review {path} and, if the identity is accepted, run "
            f"make lab-profile-activate LAB_PROFILE=<active profile> "
            f"LAB_CANDIDATE={path} CEPH_INCIDENT_LAB_ACTIVATE=1",
        )
    if report is not None and _reported_profile_hash(report) == profile.profile_hash:
        status = str(report.get("status", ""))
        if status == "preflight-pass":
            return (
                STATE_PREFLIGHT_PASSED,
                None,
                f"Hand off {report.get('directory')}: lab identity is proven for this "
                "profile, and the full real-lab gate (make validate-lab) is owned by "
                "issue #20 and is not implemented yet",
            )
        if status == "pass":
            return (
                STATE_PREFLIGHT_PASSED,
                None,
                str(report.get("next_action"))
                or f"Hand off {report.get('directory')}",
            )
        return (
            STATE_LAST_ATTEMPT_FAILED,
            f"the last attempt for this profile ended as {status}",
            str(report.get("next_action"))
            or f"Review {report.get('directory')} and fix the recorded failure",
        )
    return (
        STATE_READY_FOR_PREFLIGHT,
        None,
        f"Run make lab-preflight LAB_PROFILE={path} CEPH_INCIDENT_LAB_CONFIRM=1 to "
        "prove this workstation is talking to the recorded lab",
    )


def _reported_profile_hash(report: dict[str, object]) -> str | None:
    """The profile hash a stored report was produced for, if it recorded one.

    A report for a different profile hash is stale evidence: the profile has been
    edited or reactivated since, so its verdict no longer describes this profile.
    """

    profile = report.get("profile")
    if isinstance(profile, dict):
        recorded = profile.get("hash")
        if isinstance(recorded, str):
            return recorded
    return None


def _missing_profile_action(profile_path: Path) -> str:
    return (
        f"Create a local-only Lab Profile at {profile_path} — start from "
        "validation/lab-bootstrap.example.toml, then run "
        f"make lab-profile-discover LAB_PROFILE={profile_path}"
    )


def _credential_paths(profile: LabProfile) -> tuple[CredentialPathStatus, ...]:
    entries = (
        ("ssh key_path", profile.ssh_key_path),
        ("rook kubeconfig_path", profile.rook_kubeconfig_path),
    )
    statuses: list[CredentialPathStatus] = []
    for label, path in entries:
        problem = credential_problem(path)
        statuses.append(
            CredentialPathStatus(
                label=label,
                display=safe_display_path(path),
                usable=problem is None,
                problem=problem,
            )
        )
    return tuple(statuses)


def _candidates(profile_path: Path) -> tuple[CandidateStatus, ...]:
    """Find every candidate beside the profile, without trusting any of them."""

    directory = profile_path.parent
    if not directory.is_dir():
        return ()
    found: list[CandidateStatus] = []
    for path in sorted(directory.glob(f"*{CANDIDATE_SUFFIX}")):
        if path == profile_path:
            continue
        try:
            candidate = load_profile(path)
        except LabProfileError as error:
            found.append(
                CandidateStatus(
                    path=path, state=None, profile_hash=None, problem=str(error)
                )
            )
            continue
        found.append(
            CandidateStatus(
                path=path,
                state=candidate.state,
                profile_hash=candidate.profile_hash,
                problem=None,
            )
        )
    return tuple(found)


def _annotate_candidates(
    candidates: tuple[CandidateStatus, ...], profile: LabProfile
) -> tuple[CandidateStatus, ...]:
    """Mark a candidate whose identity the active profile already carries.

    A leftover candidate that matches the active profile is not pending review:
    activating it again would change nothing.
    """

    annotated: list[CandidateStatus] = []
    for candidate in candidates:
        matches = False
        if candidate.state == "candidate" and profile.state == "active":
            try:
                promoted = load_profile(candidate.path).with_state(
                    "active", path=profile.path
                )
                matches = promoted.profile_hash == profile.profile_hash
            except LabProfileError:
                matches = False
        annotated.append(
            CandidateStatus(
                path=candidate.path,
                state=candidate.state,
                profile_hash=candidate.profile_hash,
                problem=candidate.problem,
                matches_active=matches,
            )
        )
    return tuple(annotated)


def _last_activation(profile_path: Path) -> dict[str, object] | None:
    """Read the most recent activation record, if the ledger has one."""

    log = profile_path.parent / ACTIVATION_LOG_NAME
    try:
        lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            return record
    return None
