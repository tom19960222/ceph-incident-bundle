"""Read-only Lab Profile discovery: produce an untrusted candidate, nothing more.

A lab can be deleted and rebuilt at any time, and the new environment inherits no
trust from the old profile.  Discovery reads the new lab's identity and writes it
to a separate **Lab Profile Candidate**.  It never overwrites the active profile,
never activates anything, and never makes a candidate usable for qualification —
an operator (or an explicitly delegated agent) reviews the differences and then
runs the separate, audited activation step.

Discovery is fail-closed in one specific way: if any identity field cannot be
read, no candidate is written at all.  A partially filled candidate would look
reviewable while silently disabling the check its blank field was meant to carry.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from validation.lab_local import utc_timestamp, write_owner_only
from validation.lab_probe import HostKeyScan, LabProber, ProbeOutcome
from validation.lab_profile import LabProfile, LabProfileError, load_profile, safe_display_path


CANDIDATE_SUFFIX = ".candidate.toml"
STATUS_COMPLETE = "discovery-complete"
STATUS_INCOMPLETE = "discovery-incomplete"
STATUS_BLOCKED = "discovery-blocked"


@dataclass(frozen=True)
class DiscoveryFinding:
    """One identity field discovery tried to read from the lab."""

    subject: str
    ok: bool
    observed: str | None
    detail: str


@dataclass(frozen=True)
class DiscoveryResult:
    """What discovery observed, what it wrote, and the single next step."""

    input_path: Path
    candidate_path: Path
    status: str
    next_action: str
    candidate: LabProfile | None = None
    written: bool = False
    findings: tuple[DiscoveryFinding, ...] = ()
    differences: tuple[str, ...] = ()
    comparison_note: str = ""
    blocked_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETE

    def summary(self) -> dict[str, object]:
        """The machine-readable shape the CLI prints with `--json`."""

        return {
            "input_profile": safe_display_path(self.input_path),
            "candidate_profile": safe_display_path(self.candidate_path),
            "candidate_written": self.written,
            "candidate_hash": self.candidate.profile_hash if self.candidate else None,
            "findings": [
                {
                    "subject": finding.subject,
                    "ok": finding.ok,
                    "observed": finding.observed,
                    "detail": finding.detail,
                }
                for finding in self.findings
            ],
            "differences": list(self.differences),
            "comparison_note": self.comparison_note,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "next_action": self.next_action,
        }


def candidate_path_for(profile_path: Path) -> Path:
    """The default candidate path beside a profile: `lab.toml` -> `lab.candidate.toml`."""

    stem = profile_path.name.removesuffix(".toml").removesuffix(".candidate")
    return profile_path.with_name(f"{stem}{CANDIDATE_SUFFIX}")


def discover(
    profile_path: Path,
    *,
    candidate_path: Path | None = None,
    replace_candidate: bool = False,
) -> DiscoveryResult:
    """Run read-only discovery for one lab and write at most one candidate."""

    profile = load_profile(profile_path)
    destination = candidate_path or candidate_path_for(profile_path)
    blocked = _blocking_reason(profile_path, destination, replace_candidate)
    if blocked is not None:
        reason, action = blocked
        return DiscoveryResult(
            input_path=profile_path,
            candidate_path=destination,
            status=STATUS_BLOCKED,
            blocked_reason=reason,
            next_action=action,
        )

    with tempfile.TemporaryDirectory(prefix="ceph-incident-lab-discover.") as workspace:
        observation = _observe(profile, Path(workspace))
    findings = observation.findings
    unresolved = [finding for finding in findings if not finding.ok]
    if unresolved:
        first = unresolved[0]
        return DiscoveryResult(
            input_path=profile_path,
            candidate_path=destination,
            status=STATUS_INCOMPLETE,
            findings=findings,
            next_action=(
                f"Fix {first.subject} ({first.detail}), then re-run "
                f"make lab-profile-discover LAB_PROFILE={profile_path}"
            ),
            blocked_reason=f"could not read {first.subject}",
        )

    try:
        candidate = profile.with_identity(
            state="candidate",
            ceph_fsid=observation.ceph_fsid,
            rook_fsid=observation.rook_fsid,
            host_identity=observation.host_identity,
            path=destination,
        )
    except LabProfileError as error:
        return DiscoveryResult(
            input_path=profile_path,
            candidate_path=destination,
            status=STATUS_INCOMPLETE,
            findings=findings,
            blocked_reason=str(error),
            next_action=(
                f"Fix the discovered lab identity ({error}), then re-run "
                f"make lab-profile-discover LAB_PROFILE={profile_path}"
            ),
        )

    differences, note = _compare(profile, candidate)
    write_owner_only(destination, _candidate_document(candidate, profile_path))
    return DiscoveryResult(
        input_path=profile_path,
        candidate_path=destination,
        status=STATUS_COMPLETE,
        candidate=candidate,
        written=True,
        findings=findings,
        differences=differences,
        comparison_note=note,
        next_action=_review_action(profile_path, destination, differences),
    )


@dataclass
class _Observation:
    findings: list[DiscoveryFinding] = field(default_factory=list)
    ceph_fsid: str | None = None
    rook_fsid: str | None = None
    host_identity: dict[str, tuple[str | None, tuple[str, ...]]] = field(
        default_factory=dict
    )


def _observe(profile: LabProfile, workspace: Path) -> _Observation:
    """Probe every identity field, recording each answer rather than stopping."""

    prober = LabProber(profile, workspace=workspace)
    observation = _Observation()
    scans: list[HostKeyScan] = []
    for host in profile.hosts:
        scan = prober.scan_host_key(host)
        scans.append(scan)
        observation.findings.append(
            DiscoveryFinding(
                subject=f"host key {host.name}",
                ok=bool(scan.keys),
                observed=", ".join(scan.fingerprints) or None,
                detail=scan.detail,
            )
        )
    known_hosts = prober.write_known_hosts(scans)
    for host in profile.hosts:
        outcome = prober.read_hostname(host, known_hosts)
        observation.findings.append(
            _finding(f"hostname {host.name}", outcome)
        )
        scanned = next(scan for scan in scans if scan.host_name == host.name)
        observation.host_identity[host.name] = (outcome.value, scanned.fingerprints)
    ceph = prober.read_ceph_fsid(known_hosts)
    observation.findings.append(_finding("ceph fsid", ceph))
    observation.ceph_fsid = ceph.value
    rook = prober.read_rook_fsid()
    observation.findings.append(_finding("rook fsid", rook))
    observation.rook_fsid = rook.value
    observation.findings.append(
        _finding("prometheus readiness", prober.check_prometheus_ready())
    )
    return observation


def _finding(subject: str, outcome: ProbeOutcome) -> DiscoveryFinding:
    return DiscoveryFinding(
        subject=subject, ok=outcome.ok, observed=outcome.value, detail=outcome.detail
    )


def _blocking_reason(
    profile_path: Path, destination: Path, replace_candidate: bool
) -> tuple[str, str] | None:
    """Refuse every write that could overwrite or promote a trusted profile."""

    if destination == profile_path:
        return (
            "the candidate path is the discovery input",
            "Re-run make lab-profile-discover with a distinct "
            "--candidate-out path so the input profile is preserved",
        )
    if not destination.parent.is_dir():
        return (
            f"the candidate directory does not exist: {safe_display_path(destination.parent)}",
            f"Create {safe_display_path(destination.parent)} and re-run "
            f"make lab-profile-discover LAB_PROFILE={profile_path}",
        )
    if not destination.exists():
        return None
    existing = _existing_state(destination)
    if existing != "candidate":
        # An unreadable file is not proof that the file is disposable, so refuse
        # to overwrite it for the same reason an active profile is refused.
        described = (
            "an active Lab Profile"
            if existing == "active"
            else f"not a Lab Profile Candidate ({existing})"
        )
        return (
            f"{safe_display_path(destination)} is {described}",
            "Choose a --candidate-out path that is not an active or unreadable "
            f"profile and re-run make lab-profile-discover LAB_PROFILE={profile_path}",
        )
    if not replace_candidate:
        return (
            f"{safe_display_path(destination)} already exists",
            f"Review {safe_display_path(destination)}, then re-run "
            "make lab-profile-discover with --replace-candidate to replace it",
        )
    return None


def _existing_state(path: Path) -> str:
    """The profile state of a file already at the candidate destination."""

    try:
        return load_profile(path).state
    except LabProfileError:
        return "unreadable"


def _compare(recorded: LabProfile, candidate: LabProfile) -> tuple[tuple[str, ...], str]:
    """Name every identity difference between the input profile and the lab."""

    if _has_no_recorded_identity(recorded):
        return (), (
            "the discovery input has no recorded identity to compare against; "
            "review the candidate against the expected topology by hand"
        )
    differences: list[str] = []
    if recorded.ceph_fsid and recorded.ceph_fsid != candidate.ceph_fsid:
        differences.append(
            f"ceph fsid changed: {recorded.ceph_fsid} -> {candidate.ceph_fsid}"
        )
    if recorded.rook_fsid and recorded.rook_fsid != candidate.rook_fsid:
        differences.append(
            f"rook fsid changed: {recorded.rook_fsid} -> {candidate.rook_fsid}"
        )
    for host in candidate.hosts:
        previous = recorded.host(host.name)
        if previous.hostname and previous.hostname != host.hostname:
            differences.append(
                f"{host.name} hostname changed: {previous.hostname} -> {host.hostname}"
            )
        if previous.ssh_fingerprints and set(previous.ssh_fingerprints) != set(
            host.ssh_fingerprints
        ):
            differences.append(
                f"{host.name} host keys changed: recorded "
                f"{', '.join(previous.ssh_fingerprints)}; offered "
                f"{', '.join(host.ssh_fingerprints)}"
            )
    if differences:
        note = "the lab no longer matches the identity recorded in the discovery input"
    else:
        note = "every identity field matches the discovery input"
    return tuple(differences), note


def _has_no_recorded_identity(profile: LabProfile) -> bool:
    return (
        profile.ceph_fsid is None
        and profile.rook_fsid is None
        and all(
            not host.ssh_fingerprints and not host.hostname for host in profile.hosts
        )
    )


def _review_action(
    profile_path: Path, destination: Path, differences: tuple[str, ...]
) -> str:
    subject = (
        f"the {len(differences)} recorded difference(s) in"
        if differences
        else "the discovered identity in"
    )
    return (
        f"Review {subject} {destination} against the expected topology, then run "
        f"make lab-profile-activate LAB_PROFILE={profile_path} "
        f"LAB_CANDIDATE={destination} CEPH_INCIDENT_LAB_ACTIVATE=1"
    )


def _candidate_document(candidate: LabProfile, source: Path) -> str:
    """Render the candidate with a provenance header the loader ignores."""

    return (
        "# Lab Profile Candidate — NOT trusted, NOT usable for qualification.\n"
        f"# Produced by make lab-profile-discover at {utc_timestamp()}\n"
        f"# Discovery input: {source}\n"
        "# Review this identity, then activate it explicitly with\n"
        "# make lab-profile-activate.  This file is local-only; never commit it.\n"
        "\n" + candidate.render()
    )
