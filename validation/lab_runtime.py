"""Structured interpreter identity observations for real-lab qualification.

The schema-v3 qualification gate composes these local and node observations
without adding runtime evidence to either an Incident Bundle or a Node Evidence
Archive. Historical schema-v2 reports remain unchanged.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from validation.lab_contract import describe_differences
from validation.lab_probe import LabProber, RUNTIME_PROBE_SOURCE, bounded_diagnostic
from validation.lab_profile import LabProfile


RESULT_UNCHANGED = "unchanged"
RESULT_CHANGED = "changed"
RESULT_UNAVAILABLE = "unavailable"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
FAILURE_DETAIL_LIMIT = 50
_RUNTIME_FIELDS = frozenset({"executable", "implementation", "version_info"})
_VERSION_FIELDS = frozenset(
    {"major", "minor", "micro", "releaselevel", "serial"}
)
_RELEASE_LEVELS = frozenset({"alpha", "beta", "candidate", "final"})
_IMPLEMENTATION_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
LOCAL_PROBE_TIMEOUT_SECONDS = 30


class LocalRuntimeUnavailable(Exception):
    """A selected workstation interpreter could not provide structured facts."""


@dataclass(frozen=True)
class RuntimeVersionInfo:
    """Every stable field exposed by ``sys.version_info``."""

    major: int
    minor: int
    micro: int
    releaselevel: str
    serial: int

    def document(self) -> dict[str, object]:
        return {
            "major": self.major,
            "minor": self.minor,
            "micro": self.micro,
            "releaselevel": self.releaselevel,
            "serial": self.serial,
        }


@dataclass(frozen=True)
class RuntimeIdentity:
    """The resolved executable, implementation and complete version tuple."""

    executable: str
    implementation: str
    version_info: RuntimeVersionInfo

    def document(self) -> dict[str, object]:
        return {
            "executable": self.executable,
            "implementation": self.implementation,
            "version_info": self.version_info.document(),
        }


def current_runtime_identity() -> RuntimeIdentity:
    """Describe the interpreter that is actually running the lab harness."""

    version = sys.version_info
    return RuntimeIdentity(
        os.path.abspath(sys.executable),
        sys.implementation.name,
        RuntimeVersionInfo(
            version.major,
            version.minor,
            version.micro,
            version.releaselevel,
            version.serial,
        ),
    )


def read_local_runtime_identity(executable: Path) -> RuntimeIdentity:
    """Run one fixed metadata-only probe through the explicitly selected path."""

    selected = str(executable)
    if not executable.is_absolute():
        raise LocalRuntimeUnavailable(
            f"production interpreter must be selected by absolute path: {selected}"
        )
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise LocalRuntimeUnavailable(
            f"production interpreter is missing or not executable: {selected}"
        )
    try:
        completed = subprocess.run(
            [selected, "-I", "-B", "-S", "-c", RUNTIME_PROBE_SOURCE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=LOCAL_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalRuntimeUnavailable(
            f"production interpreter probe failed: {type(error).__name__}"
        ) from error
    if completed.returncode != 0:
        detail = bounded_diagnostic(completed.stderr) or "no diagnostic"
        raise LocalRuntimeUnavailable(
            f"production interpreter probe exited {completed.returncode}: {detail}"
        )
    try:
        return parse_runtime_identity(completed.stdout)
    except ValueError as error:
        raise LocalRuntimeUnavailable(
            "production interpreter returned malformed runtime identity"
        ) from error


@dataclass(frozen=True)
class RuntimeProbe:
    """One inventory node's explicit probe exit, status and parsed identity."""

    host: str
    exit_code: int
    status: str
    runtime: RuntimeIdentity | None
    detail: str

    def document(self) -> dict[str, object]:
        return {
            "host": self.host,
            "exit_code": self.exit_code,
            "status": self.status,
            "runtime": None if self.runtime is None else self.runtime.document(),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Every inventory node's runtime observation at one lab moment."""

    probes: tuple[RuntimeProbe, ...]

    def document(self) -> dict[str, object]:
        return {"probes": [probe.document() for probe in self.probes]}

    def structured_fields(self) -> dict[str, object]:
        """Fields whose pre/post stability the runtime gate promises."""

        return {
            probe.host: None if probe.runtime is None else probe.runtime.document()
            for probe in self.probes
        }


def capture_runtime_snapshot(
    prober: LabProber, profile: LabProfile, known_hosts: Path
) -> RuntimeSnapshot:
    """Probe every inventory node, preserving failures instead of stopping early."""

    probes: list[RuntimeProbe] = []
    for host in profile.hosts:
        outcome = prober.read_runtime_identity(host, known_hosts)
        exit_code = outcome.exit_code if outcome.exit_code is not None else 1
        if not outcome.ok or outcome.value is None:
            probes.append(
                RuntimeProbe(
                    host.name,
                    exit_code,
                    STATUS_FAILED,
                    None,
                    outcome.detail,
                )
            )
            continue
        try:
            runtime = parse_runtime_identity(outcome.value)
        except ValueError:
            probes.append(
                RuntimeProbe(
                    host.name,
                    exit_code,
                    STATUS_FAILED,
                    None,
                    f"runtime probe on {host.name}: exit {exit_code}: malformed identity",
                )
            )
            continue
        probes.append(
            RuntimeProbe(host.name, exit_code, STATUS_OK, runtime, outcome.detail)
        )
    return RuntimeSnapshot(tuple(probes))


def compare_runtime_snapshots(
    before: RuntimeSnapshot, after: RuntimeSnapshot
) -> tuple[str, tuple[str, ...]]:
    """Fail closed on an unreadable node, otherwise name every runtime drift."""

    unavailable = [
        f"{moment}.{probe.host}: {probe.detail}"
        for moment, snapshot in (("before", before), ("after", after))
        for probe in snapshot.probes
        if probe.status != STATUS_OK or probe.runtime is None
    ]
    if unavailable:
        hidden = len(unavailable) - FAILURE_DETAIL_LIMIT
        bounded = unavailable[:FAILURE_DETAIL_LIMIT]
        if hidden > 0:
            bounded.append(f"… and {hidden} further failed probe(s) not listed")
        return RESULT_UNAVAILABLE, tuple(bounded)
    differences = describe_differences(
        before.structured_fields(), after.structured_fields()
    )
    return (RESULT_CHANGED if differences else RESULT_UNCHANGED), differences


def parse_runtime_identity(text: str) -> RuntimeIdentity:
    """Parse exactly the fixed probe schema, rejecting ambiguity or omissions."""

    try:
        document = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ValueError("runtime identity is not JSON") from error
    if not isinstance(document, dict) or frozenset(document) != _RUNTIME_FIELDS:
        raise ValueError("runtime identity fields do not match the fixed schema")
    executable = document["executable"]
    implementation = document["implementation"]
    version = document["version_info"]
    if (
        not isinstance(executable, str)
        or not executable.startswith("/")
        or "\x00" in executable
    ):
        raise ValueError("runtime executable is not an absolute path")
    if (
        not isinstance(implementation, str)
        or _IMPLEMENTATION_NAME.fullmatch(implementation) is None
    ):
        raise ValueError("runtime implementation name is invalid")
    if not isinstance(version, dict) or frozenset(version) != _VERSION_FIELDS:
        raise ValueError("version_info fields do not match the fixed schema")
    for field in ("major", "minor", "micro", "serial"):
        value = version[field]
        if type(value) is not int or value < 0:
            raise ValueError(f"version_info.{field} is not a non-negative integer")
    releaselevel = version["releaselevel"]
    if not isinstance(releaselevel, str) or releaselevel not in _RELEASE_LEVELS:
        raise ValueError("version_info.releaselevel is invalid")
    return RuntimeIdentity(
        executable,
        implementation,
        RuntimeVersionInfo(
            version["major"],
            version["minor"],
            version["micro"],
            releaselevel,
            version["serial"],
        ),
    )
