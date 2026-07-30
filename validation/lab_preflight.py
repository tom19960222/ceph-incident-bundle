"""Strict, fail-closed lab identity preflight.

This is the gate that must pass before any real-lab collect starts: profile
shape, credential paths, SSH host key fingerprints, required hosts, Ceph and Rook
FSIDs, and Prometheus readiness.  Every stage runs in order and the first failing
stage stops the run, because a later probe's answer is not evidence once we no
longer know which machine answered it.

Nothing here can be waived.  There is no accept-current mode, no skip flag, and
no path that rewrites the active profile to make a mismatch go away: a rebuilt
lab has to go back through discovery, review and explicit activation.  Whether a
collect invocation keeps `cephadm shell` and `kubectl exec` disabled is checked
where that invocation is built (issue #20), not here.
"""

from __future__ import annotations

import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from validation.lab_commands import (
    activate_command,
    discover_command,
    qualification_not_implemented,
)
from validation.lab_probe import LabProber
from validation.lab_profile import LabProfile, load_profile, safe_display_path


STATUS_PASS = "preflight-pass"
FAILURE_PROFILE_NOT_ACTIVE = "profile-not-active"
FAILURE_CREDENTIAL_PATH = "credential-path-invalid"
FAILURE_FINGERPRINT = "ssh-fingerprint-mismatch"
FAILURE_HOST_UNREACHABLE = "host-unreachable"
FAILURE_HOST_IDENTITY = "host-identity-mismatch"
FAILURE_CEPH_IDENTITY = "ceph-identity-mismatch"
FAILURE_ROOK_IDENTITY = "rook-identity-mismatch"
FAILURE_PROMETHEUS = "prometheus-not-ready"
# The gate that consumes a passing preflight does not exist yet.  Saying so is
# part of the contract: a passing preflight is not qualification evidence.
PASS_NEXT_ACTION = (
    f"Hand off this preflight report: {qualification_not_implemented()}"
)


@dataclass(frozen=True)
class PreflightCheck:
    """One preflight stage's verdict."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreflightResult:
    """Whether this workstation is talking to the lab the profile describes."""

    profile_path: Path
    profile: LabProfile
    status: str
    next_action: str
    checks: tuple[PreflightCheck, ...] = ()
    identity: dict[str, object] = field(default_factory=dict)
    blocked_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_PASS

    def summary(self) -> dict[str, object]:
        return {
            "profile": safe_display_path(self.profile_path),
            "profile_hash": self.profile.profile_hash,
            "profile_state": self.profile.state,
            "checks": [
                {"name": check.name, "ok": check.ok, "detail": check.detail}
                for check in self.checks
            ],
            "verified_identity": self.identity,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "next_action": self.next_action,
        }


def preflight(profile_path: Path) -> PreflightResult:
    """Run every identity check in order, stopping at the first failure."""

    profile = load_profile(profile_path)
    run = _Preflight(profile_path, profile)
    return run.execute()


class _Preflight:
    """One preflight run: accumulated checks, verified identity, single verdict."""

    def __init__(self, profile_path: Path, profile: LabProfile) -> None:
        self.profile_path = profile_path
        self.profile = profile
        self.checks: list[PreflightCheck] = []
        self.identity: dict[str, object] = {}
        # Written once the fingerprint stage passes; every later probe connects
        # through it, so no probe can run before host keys are verified.
        self.known_hosts = Path()

    def execute(self) -> PreflightResult:
        state = self._check_profile_state()
        if state is not None:
            return state
        credentials = self._check_credential_paths()
        if credentials is not None:
            return credentials
        with tempfile.TemporaryDirectory(prefix="ceph-incident-lab-preflight.") as workspace:
            prober = LabProber(self.profile, workspace=Path(workspace))
            for stage in (
                self._check_fingerprints,
                self._check_hosts,
                self._check_ceph_identity,
                self._check_rook_identity,
                self._check_prometheus,
            ):
                failure = stage(prober)
                if failure is not None:
                    return failure
        return PreflightResult(
            profile_path=self.profile_path,
            profile=self.profile,
            status=STATUS_PASS,
            checks=tuple(self.checks),
            identity=self.identity,
            next_action=PASS_NEXT_ACTION,
        )

    def _passed(self, name: str, detail: str) -> None:
        self.checks.append(PreflightCheck(name=name, ok=True, detail=detail))

    def _failed(
        self, name: str, detail: str, failure_class: str, next_action: str
    ) -> PreflightResult:
        self.checks.append(PreflightCheck(name=name, ok=False, detail=detail))
        return PreflightResult(
            profile_path=self.profile_path,
            profile=self.profile,
            status=failure_class,
            checks=tuple(self.checks),
            identity=self.identity,
            blocked_reason=detail,
            next_action=next_action,
        )

    def _check_profile_state(self) -> PreflightResult | None:
        if self.profile.state == "active":
            self._passed(
                "profile-state",
                f"active profile {self.profile.profile_hash} with "
                f"{len(self.profile.hosts)} host(s)",
            )
            return None
        if self.profile.state == "candidate":
            action = (
                f"Review {self.profile_path} and, if the identity is accepted, run "
                + activate_command("<active profile>", self.profile_path)
            )
        else:
            action = (
                f"Run {discover_command(self.profile_path)} to produce a Lab Profile "
                "Candidate, then review and activate it"
            )
        return self._failed(
            "profile-state",
            f"{safe_display_path(self.profile_path)} is a {self.profile.state} profile; "
            "qualification requires an explicitly activated profile",
            FAILURE_PROFILE_NOT_ACTIVE,
            action,
        )

    def _check_credential_paths(self) -> PreflightResult | None:
        """Prove the credential files exist and are private, never read them."""

        for label, path in self.profile.credential_paths():
            problem = credential_problem(path)
            if problem is not None:
                return self._failed(
                    "credential-paths",
                    f"{label} {safe_display_path(path)} {problem}",
                    FAILURE_CREDENTIAL_PATH,
                    f"Fix {label} in {self.profile_path} — {path} {problem} — then "
                    "re-run the preflight",
                )
        self._passed(
            "credential-paths",
            "ssh key and kubeconfig exist and are readable only by their owner",
        )
        return None

    def _check_fingerprints(self, prober: LabProber) -> PreflightResult | None:
        """Every key a host offers must already be trusted by the profile."""

        scans = [prober.scan_host_key(host) for host in self.profile.hosts]
        problems: list[str] = []
        for host, scan in zip(self.profile.hosts, scans):
            if not scan.keys:
                problems.append(f"{host.name}: {scan.detail}")
                continue
            untrusted = [
                fingerprint
                for fingerprint in scan.fingerprints
                if fingerprint not in host.ssh_fingerprints
            ]
            if untrusted:
                problems.append(
                    f"{host.name} offered untrusted host key(s) {', '.join(untrusted)}; "
                    f"profile records {', '.join(host.ssh_fingerprints)}"
                )
        if problems:
            return self._failed(
                "ssh-fingerprints",
                "; ".join(problems),
                FAILURE_FINGERPRINT,
                f"Run {discover_command(self.profile_path)} to record the offered "
                "host keys, review the difference, then activate the candidate — "
                "never edit the recorded fingerprints to match",
            )
        # Only keys the active profile already trusts reach the pinned known_hosts,
        # so every later probe is talking to a host that proved a trusted key.
        self.known_hosts = prober.write_known_hosts(
            scans,
            accepted={host.name: host.ssh_fingerprints for host in self.profile.hosts},
        )
        self.identity["hosts"] = [
            {
                "name": host.name,
                "address": host.address,
                "hostname": None,
                "ssh_fingerprints_verified": list(scan.fingerprints),
            }
            for host, scan in zip(self.profile.hosts, scans)
        ]
        self._passed(
            "ssh-fingerprints",
            f"{len(scans)} host(s) offered only host keys the profile trusts",
        )
        return None

    def _check_hosts(self, prober: LabProber) -> PreflightResult | None:
        recorded_hosts: list[dict[str, object]] = self.identity["hosts"]  # type: ignore[assignment]
        for index, host in enumerate(self.profile.hosts):
            outcome = prober.read_hostname(host, self.known_hosts)
            if not outcome.ok:
                return self._failed(
                    "required-hosts",
                    f"{host.name} ({host.address}) did not answer: {outcome.detail}",
                    FAILURE_HOST_UNREACHABLE,
                    f"Restore read-only SSH access to {host.name} "
                    f"({outcome.detail}) and re-run the preflight",
                )
            if outcome.value != host.hostname:
                return self._failed(
                    "required-hosts",
                    f"{host.name} ({host.address}) reports hostname {outcome.value}; "
                    f"profile records {host.hostname}",
                    FAILURE_HOST_IDENTITY,
                    f"Reconcile the host map: {host.address} is "
                    f"{outcome.value}, not {host.hostname} — fix the lab or re-run "
                    "discovery and activation, then re-run the preflight",
                )
            recorded_hosts[index]["hostname"] = outcome.value
        self._passed(
            "required-hosts",
            f"all {len(self.profile.hosts)} required host(s) answered with the "
            "recorded hostname",
        )
        return None

    def _check_ceph_identity(self, prober: LabProber) -> PreflightResult | None:
        outcome = prober.read_ceph_fsid(self.known_hosts)
        if not outcome.ok:
            return self._failed(
                "ceph-identity",
                f"could not read the Ceph FSID from {self.profile.ceph_seed}: "
                f"{outcome.detail}",
                FAILURE_CEPH_IDENTITY,
                f"Restore direct read-only Ceph CLI access on "
                f"{self.profile.ceph_seed} ({outcome.detail}) and re-run the preflight",
            )
        if outcome.value != self.profile.ceph_fsid:
            return self._failed(
                "ceph-identity",
                f"{self.profile.ceph_seed} reports Ceph FSID {outcome.value}; "
                f"profile records {self.profile.ceph_fsid}",
                FAILURE_CEPH_IDENTITY,
                "This is a different Ceph cluster: run "
                f"{discover_command(self.profile_path)}, review the new identity, "
                "and activate the candidate before any collect",
            )
        self.identity["ceph_fsid"] = outcome.value
        self._passed("ceph-identity", f"Ceph FSID matches via {outcome.detail}")
        return None

    def _check_rook_identity(self, prober: LabProber) -> PreflightResult | None:
        outcome = prober.read_rook_fsid()
        if not outcome.ok:
            return self._failed(
                "rook-identity",
                f"could not read the Rook CephCluster FSID: {outcome.detail}",
                FAILURE_ROOK_IDENTITY,
                f"Restore local read-only kubectl access to namespace "
                f"{self.profile.rook_namespace} ({outcome.detail}) and re-run the "
                "preflight",
            )
        if outcome.value != self.profile.rook_fsid:
            return self._failed(
                "rook-identity",
                f"namespace {self.profile.rook_namespace} reports Rook FSID "
                f"{outcome.value}; profile records {self.profile.rook_fsid}",
                FAILURE_ROOK_IDENTITY,
                "This is a different Rook cluster: run "
                f"{discover_command(self.profile_path)}, review the new identity, "
                "and activate the candidate before any collect",
            )
        self.identity["rook_fsid"] = outcome.value
        self._passed("rook-identity", f"Rook FSID matches via {outcome.detail}")
        return None

    def _check_prometheus(self, prober: LabProber) -> PreflightResult | None:
        outcome = prober.check_prometheus_ready()
        if not outcome.ok:
            return self._failed(
                "prometheus-readiness",
                f"{self.profile.prometheus_url} is not ready: {outcome.detail}",
                FAILURE_PROMETHEUS,
                f"Restore the Prometheus endpoint {self.profile.prometheus_url} "
                f"({outcome.detail}) and re-run the preflight",
            )
        self.identity["prometheus"] = {
            "url": self.profile.prometheus_url,
            "ready": True,
        }
        self._passed("prometheus-readiness", outcome.detail)
        return None


def credential_problem(path: Path) -> str | None:
    """Describe why a credential path is unusable, without reading its content.

    `lab-status` reports the same verdict locally, so this stays the single
    definition of what makes a referenced credential file usable.
    """

    try:
        info = path.lstat()
    except OSError:
        return "does not exist"
    if stat.S_ISLNK(info.st_mode):
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return "is a symlink to a missing file"
        return credential_problem(resolved)
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if info.st_mode & 0o077:
        return f"is readable by other users (mode {info.st_mode & 0o777:04o})"
    return None
