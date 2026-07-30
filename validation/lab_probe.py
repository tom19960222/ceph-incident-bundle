"""Read-only lab probes shared by discovery and strict identity preflight.

Every probe here is a query: `ssh-keyscan`, `hostname`, `ceph fsid`, a local
`kubectl get`, and one Prometheus readiness GET.  Nothing in this module may
create, change or delete lab state — see `docs/read-only-safety.md`.

Two rules shape the SSH surface.  Commands are built as argv vectors, never as a
local shell expression, so no profile value can become a shell token.  And every
SSH connection pins host keys through a collector-owned `known_hosts` file with
`StrictHostKeyChecking=yes`; the operator's own `known_hosts` is never read or
written, and `accept-new` is never used.  Which keys land in that file is the
caller's decision: discovery writes everything it scanned (its output is an
untrusted candidate), while preflight writes only keys the active profile
already trusts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from validation.lab_profile import CREDENTIAL_MARKERS, LabHost, LabProfile


DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
# ssh-keyscan is bounded separately: it is the only probe that runs before any
# host key is trusted, so it must never be able to hang the workflow.
DEFAULT_KEYSCAN_TIMEOUT_SECONDS = 10
# Probe answers are identifiers and short status text.  Anything longer is not an
# answer this module knows how to use.
PROBE_OUTPUT_CAP_BYTES = 64 * 1024
DIAGNOSTIC_MAX_LENGTH = 200
EXIT_COMMAND_MISSING = 127
EXIT_TIMEOUT = 124
KEYSCAN_KEY_TYPE = re.compile(r"[a-z][a-z0-9@._-]*\Z")
KEYSCAN_BLOB = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")
KNOWN_HOSTS_NAME = "known_hosts"


@dataclass(frozen=True)
class HostKey:
    """One host key a lab host offered, identified by its SHA256 fingerprint."""

    key_type: str
    fingerprint: str
    known_hosts_line: str


@dataclass(frozen=True)
class HostKeyScan:
    """The result of one read-only host key scan."""

    host_name: str
    address: str
    keys: tuple[HostKey, ...]
    detail: str

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(key.fingerprint for key in self.keys)


@dataclass(frozen=True)
class ProbeOutcome:
    """One probe's answer plus a bounded, secret-free diagnostic."""

    ok: bool
    value: str | None
    detail: str


def fingerprint_of(blob: str) -> str:
    """Render the OpenSSH SHA256 fingerprint of one base64 host key blob."""

    digest = hashlib.sha256(base64.b64decode(blob, validate=True)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def bounded_diagnostic(text: str) -> str:
    """Keep just enough of a command's complaint to locate the problem."""

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in CREDENTIAL_MARKERS):
            return "[redacted diagnostic]"
        return stripped[:DIAGNOSTIC_MAX_LENGTH]
    return ""


class LabProber:
    """Runs the read-only probes one lab profile describes."""

    def __init__(
        self,
        profile: LabProfile,
        *,
        workspace: Path,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        keyscan_timeout: int = DEFAULT_KEYSCAN_TIMEOUT_SECONDS,
    ) -> None:
        self.profile = profile
        self.workspace = workspace
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.keyscan_timeout = keyscan_timeout

    def scan_host_key(self, host: LabHost) -> HostKeyScan:
        """Scan one host's offered keys without trusting or recording them."""

        code, out, err = _run(
            ["ssh-keyscan", "-T", str(self.keyscan_timeout), "--", host.address],
            timeout=self.keyscan_timeout + 5,
        )
        keys: list[HostKey] = []
        rejected = 0
        for line in out.splitlines():
            key = _host_key_of(line, host.address)
            if key is None:
                rejected += 1
                continue
            if all(key.fingerprint != seen.fingerprint for seen in keys):
                keys.append(key)
        if keys:
            detail = f"{len(keys)} host key(s) offered"
            if rejected:
                detail += f"; {rejected} unusable scan line(s) ignored"
            return HostKeyScan(host.name, host.address, tuple(keys), detail)
        detail = bounded_diagnostic(err) or f"ssh-keyscan exited {code}"
        return HostKeyScan(host.name, host.address, (), f"no host key offered: {detail}")

    def write_known_hosts(
        self,
        scans: Sequence[HostKeyScan],
        *,
        accepted: Mapping[str, tuple[str, ...]] | None = None,
    ) -> Path:
        """Write the collector-owned known_hosts file for the keys we may trust.

        With `accepted`, only fingerprints the caller already trusts are written,
        so a host that rotated its key cannot be reached at all rather than being
        reached and then reported as a mismatch.

        The name is fixed rather than a parameter: this file is the workspace's
        one trust anchor, and a caller-chosen name could point outside it.
        """

        lines: list[str] = []
        for scan in scans:
            allowed = None if accepted is None else accepted.get(scan.host_name, ())
            for key in scan.keys:
                if allowed is None or key.fingerprint in allowed:
                    lines.append(key.known_hosts_line)
        path = self.workspace / KNOWN_HOSTS_NAME
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            for line in lines:
                output.write(line + "\n")
        return path

    def read_hostname(self, host: LabHost, known_hosts: Path) -> ProbeOutcome:
        """Read one host's own hostname, to confirm the host map points at it."""

        code, out, err = _run(
            self._ssh_command(host, known_hosts, "hostname"),
            timeout=self.command_timeout,
        )
        value = out.strip()
        if code == 0 and value:
            return ProbeOutcome(True, value, f"hostname reported as {value}")
        return ProbeOutcome(False, None, _failure_detail("hostname", code, err))

    def read_ceph_fsid(self, known_hosts: Path) -> ProbeOutcome:
        """Read the Ceph FSID from the seed host using a direct read-only CLI.

        `cephadm shell` can start a container, so it is not a fallback here; only
        `ceph` and `sudo -n ceph` are tried, exactly as the collectors do.
        """

        seed = self.profile.seed_host
        attempts: list[str] = []
        for remote in ("ceph fsid", "sudo -n ceph fsid"):
            code, out, err = _run(
                self._ssh_command(seed, known_hosts, remote),
                timeout=self.command_timeout,
            )
            value = out.strip()
            if code == 0 and value:
                return ProbeOutcome(True, value.lower(), f"`{remote}` on {seed.name}")
            attempts.append(_failure_detail(remote, code, err))
        return ProbeOutcome(False, None, "; ".join(attempts))

    def read_rook_fsid(self) -> ProbeOutcome:
        """Read the Rook CephCluster FSID with one local read-only kubectl get."""

        namespace = self.profile.rook_namespace
        code, out, err = _run(
            [
                "kubectl",
                "--kubeconfig",
                str(self.profile.rook_kubeconfig_path),
                "-n",
                namespace,
                "get",
                "cephclusters.ceph.rook.io",
                "-o",
                "json",
            ],
            timeout=self.command_timeout,
        )
        if code != 0:
            return ProbeOutcome(False, None, _failure_detail("kubectl get", code, err))
        try:
            document = json.loads(out)
            items = document["items"]
        except (ValueError, KeyError, TypeError):
            return ProbeOutcome(False, None, "kubectl returned an unparseable CephCluster list")
        if not isinstance(items, list) or not items:
            return ProbeOutcome(
                False, None, f"no CephCluster in namespace {namespace}"
            )
        fsids: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                return ProbeOutcome(False, None, "malformed CephCluster entry")
            fsid = _nested(item, ("status", "ceph", "fsid"))
            if isinstance(fsid, str) and fsid:
                fsids.add(fsid.lower())
        if not fsids:
            return ProbeOutcome(
                False, None, f"no CephCluster reports status.ceph.fsid in {namespace}"
            )
        if len(fsids) > 1:
            return ProbeOutcome(
                False,
                None,
                f"namespace {namespace} reports {len(fsids)} different CephCluster FSIDs",
            )
        return ProbeOutcome(True, fsids.pop(), f"CephCluster in namespace {namespace}")

    def check_prometheus_ready(self) -> ProbeOutcome:
        """Confirm the Prometheus endpoint answers its readiness probe."""

        url = f"{self.profile.prometheus_url}/-/ready"
        body = self.workspace / "prometheus-ready.txt"
        code, _, err = _run(
            [
                "curl",
                "-q",
                "-fsS",
                "--connect-timeout",
                str(self.connect_timeout),
                "--max-time",
                str(self.connect_timeout),
                "-o",
                str(body),
                url,
            ],
            timeout=self.connect_timeout + 5,
        )
        if code != 0:
            return ProbeOutcome(False, None, _failure_detail("curl", code, err))
        return ProbeOutcome(True, "ready", bounded_diagnostic(_read_capped(body)) or "ready")

    def _ssh_command(self, host: LabHost, known_hosts: Path, remote: str) -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.profile.ssh_key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "LogLevel=ERROR",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            self.profile.ssh_target(host),
            remote,
        ]


def _host_key_of(line: str, address: str) -> HostKey | None:
    """Accept one ssh-keyscan line, or nothing.

    A scan line becomes a known_hosts entry, so anything that is not exactly
    `<scanned address> <key type> <base64 blob>` is dropped: a marker line, a
    `@cert-authority` directive or an extra field must not be able to widen what
    the pinned known_hosts file trusts.
    """

    if line.startswith("#"):
        return None
    fields = line.split(" ")
    if len(fields) != 3:
        return None
    scanned, key_type, blob = fields
    if scanned != address:
        return None
    if KEYSCAN_KEY_TYPE.fullmatch(key_type) is None or KEYSCAN_BLOB.fullmatch(blob) is None:
        return None
    try:
        fingerprint = fingerprint_of(blob)
    except (ValueError, TypeError):
        return None
    return HostKey(key_type, fingerprint, f"{scanned} {key_type} {blob}")


def _nested(document: Mapping[str, object], keys: Sequence[str]) -> object:
    current: object = document
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _failure_detail(label: str, code: int, stderr: str) -> str:
    if code == EXIT_COMMAND_MISSING:
        return f"{label}: command not found on the workstation"
    if code == EXIT_TIMEOUT:
        return f"{label}: timed out"
    detail = bounded_diagnostic(stderr)
    return f"{label}: exit {code}" + (f": {detail}" if detail else "")


def _read_capped(path: Path) -> str:
    try:
        with open(path, "rb") as source:
            return source.read(PROBE_OUTPUT_CAP_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""


def _run(command: Sequence[str], *, timeout: int) -> tuple[int, str, str]:
    """Run one bounded probe and normalise transport failures into exit codes."""

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return EXIT_COMMAND_MISSING, "", ""
    except subprocess.TimeoutExpired:
        return EXIT_TIMEOUT, "", ""
    except OSError as error:
        return EXIT_COMMAND_MISSING, "", str(error)
    return (
        completed.returncode,
        completed.stdout[:PROBE_OUTPUT_CAP_BYTES].decode("utf-8", "replace"),
        completed.stderr[:PROBE_OUTPUT_CAP_BYTES].decode("utf-8", "replace"),
    )
