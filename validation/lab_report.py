"""The Lab Validation Report: one run's persisted, machine-readable verdict.

Every real-lab attempt — pass or fail — leaves the same pair of documents in one
run directory, `report.md` for people and `report.json` for tooling, with a
local-only `LATEST` pointer naming the most recent run.  A later agent reads the
report instead of a chat log.

Two invariants are enforced here rather than trusted.  A report carries exactly
one `next_action`, because a list of competing suggestions is how a gate gets
bypassed.  And a report is scanned for credential material before it is written:
if a diagnostic ever carries a key, header or token, the write fails closed
instead of persisting the leak.

An identity preflight fills only the identity, preflight and status fields; the
post-cutover gate (`validation/lab_qualify.py`) fills the rest.  Whatever a run did
not reach stays `not-run`, so a report says where the gate stopped rather than
only that it did.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from validation.lab_output import utc_timestamp, write_owner_only
from validation.lab_preflight import PreflightResult
from validation.lab_profile import CREDENTIAL_MARKERS, safe_display_path
from validation.lab_runtime import (
    RuntimeIdentity,
    RuntimeSnapshot,
    parse_runtime_identity,
)

if TYPE_CHECKING:  # a report describes a qualification; it must not import one
    from validation.lab_qualify import QualifyResult


REPORT_SCHEMA_VERSION = 3
REPORT_JSON_NAME = "report.json"
REPORT_MARKDOWN_NAME = "report.md"
LATEST_NAME = "LATEST"
NOT_RUN = "not-run"
STATUS_PASS = "pass"
COLLECTOR_PATHS = ("ceph", "rook", "prometheus", "nodes", "var_log")
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
GIT_TIMEOUT_SECONDS = 10
REQUIRED_QUALIFICATION_CHECKS = frozenset(
    {
        "code-identity",
        "read-only-opt-ins",
        "runtime-tooling",
        "runtime-production",
        "baseline-evidence",
        "profile-state",
        "credential-paths",
        "ssh-fingerprints",
        "required-hosts",
        "ceph-identity",
        "rook-identity",
        "prometheus-readiness",
        "baseline-identity",
        "shared-inventory",
        "runtime-node-pre",
        "runtime-floor-witness",
        "stable-state-pre",
        "residue-baseline",
        "code-identity-pre-collect",
        "collect-python",
        "runtime-floor-witness-collect",
        "collector-coverage-python",
        "workstation-cleanup-python",
        "bundle-comparison",
        "stable-state-post",
        "runtime-node-post",
        "remote-residue",
        "runtime-floor-witness-proof",
        "code-identity-final",
    }
)


class ReportRejected(Exception):
    """A report was refused rather than written in an unsafe or ambiguous shape."""


@dataclass(frozen=True)
class CodeIdentity:
    """Which code a lab workflow ran, so a report can be traced back to it."""

    commit: str
    dirty: bool

    @property
    def display(self) -> str:
        return f"{self.commit}{'-dirty' if self.dirty else ''}"


def code_identity(root: Path) -> CodeIdentity:
    """Read the repository commit and dirty state, or report them as unknown."""

    commit = _git(root, "rev-parse", "HEAD") or "unknown"
    status = _git(root, "status", "--porcelain")
    return CodeIdentity(commit=commit, dirty=bool(status))


def tracked_modifications(root: Path) -> tuple[str, ...]:
    """The tracked files that differ from HEAD, ignoring untracked ones.

    The qualification gate needs this stricter reading than `CodeIdentity.dirty`: a
    modified tracked file means the recorded commit does not describe the code
    that ran, while an untracked file — a local-only Lab Profile beside the
    repository, say — changes nothing about it.
    """

    status = _git(root, "status", "--porcelain")
    # Porcelain v1 is `XY <path>`; slicing past the two status columns and
    # stripping keeps the path whole whether the change is staged, unstaged or
    # both.
    return tuple(
        line[2:].strip()
        for line in status.splitlines()
        if line.strip() and not line.startswith("??")
    )


def _git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


@dataclass(frozen=True)
class CollectorCoverage:
    """Whether one invocation covered each of the four collector paths."""

    ceph: str = NOT_RUN
    rook: str = NOT_RUN
    prometheus: str = NOT_RUN
    nodes: str = NOT_RUN
    var_log: str = NOT_RUN

    def document(self) -> dict[str, str]:
        return {path: getattr(self, path) for path in COLLECTOR_PATHS}

    @property
    def complete(self) -> bool:
        return not self.gaps()

    def gaps(self) -> tuple[str, ...]:
        """Name each collector path this invocation did not cover, and how."""

        return tuple(
            f"{path}={getattr(self, path)}"
            for path in COLLECTOR_PATHS
            if getattr(self, path) != "collected"
        )


@dataclass(frozen=True)
class RunRecord:
    """One full collect: which implementation ran it, and what it produced."""

    implementation: str
    exit_code: int | None = None
    invocation_id: str | None = None
    bundle_path: str | None = None
    bundle_hash: str | None = None
    verify_result: str = NOT_RUN
    coverage: CollectorCoverage = field(default_factory=CollectorCoverage)

    def document(self) -> dict[str, object]:
        return {
            "implementation": self.implementation,
            "exit_code": self.exit_code,
            "invocation_id": self.invocation_id,
            "bundle_path": self.bundle_path,
            "bundle_hash": self.bundle_hash,
            "verify_result": self.verify_result,
            "coverage": self.coverage.document(),
        }


@dataclass(frozen=True)
class BaselineRecord:
    """The immutable #21 evidence selected for a post-cutover proof."""

    status: str = NOT_RUN
    report_path: str | None = None
    report_hash: str | None = None
    code_commit: str | None = None
    profile_hash: str | None = None
    shell_bundle_path: str | None = None
    shell_bundle_hash: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "report_path": self.report_path,
            "report_hash": self.report_hash,
            "code_commit": self.code_commit,
            "profile_hash": self.profile_hash,
            "shell_bundle_path": self.shell_bundle_path,
            "shell_bundle_hash": self.shell_bundle_hash,
        }


@dataclass(frozen=True)
class ComparisonRecord:
    """The normalized observable-contract comparison of the two bundles."""

    result: str = NOT_RUN
    differences: tuple[str, ...] = ()

    def document(self) -> dict[str, object]:
        return {"result": self.result, "differences": list(self.differences)}


@dataclass(frozen=True)
class StableStateRecord:
    """The pre/post stable state snapshot comparison."""

    schema_version: int | None = None
    result: str = NOT_RUN
    differences: tuple[str, ...] = ()

    def document(self) -> dict[str, object]:
        return {
            "snapshot_schema_version": self.schema_version,
            "result": self.result,
            "differences": list(self.differences),
        }


@dataclass(frozen=True)
class RuntimeProofRecord:
    """Structured interpreter proof kept outside every evidence bundle."""

    tooling: RuntimeIdentity | None = None
    production: RuntimeIdentity | None = None
    nodes_pre: RuntimeSnapshot | None = None
    nodes_post: RuntimeSnapshot | None = None
    comparison_result: str = NOT_RUN
    differences: tuple[str, ...] = ()
    floor_witness: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "tooling": None if self.tooling is None else self.tooling.document(),
            "production": (
                None if self.production is None else self.production.document()
            ),
            "nodes": {
                "pre": None if self.nodes_pre is None else self.nodes_pre.document(),
                "post": None if self.nodes_post is None else self.nodes_post.document(),
            },
            "comparison": {
                "result": self.comparison_result,
                "differences": list(self.differences),
            },
            "floor_witness": self.floor_witness,
        }


@dataclass(frozen=True)
class ResidueRecord:
    """One node's remote residue check, scoped to this run's invocations."""

    host: str
    result: str = NOT_RUN
    detail: str = ""

    def document(self) -> dict[str, object]:
        return {"host": self.host, "result": self.result, "detail": self.detail}


@dataclass(frozen=True)
class LabValidationReport:
    """One real-lab attempt's persisted result."""

    timestamp: str
    code: CodeIdentity
    profile_display: str
    # A profile that would not load has no hash, state or name to record.  The
    # attempt is still reported: "the profile was unusable" is the result.
    profile_hash: str | None
    profile_state: str | None
    profile_name: str | None
    status: str
    next_action: str
    lab_identity: dict[str, object] = field(default_factory=dict)
    preflight: tuple[dict[str, object], ...] = ()
    baseline: BaselineRecord = field(default_factory=BaselineRecord)
    runs: tuple[RunRecord, ...] = ()
    comparison: ComparisonRecord = field(default_factory=ComparisonRecord)
    stable_state: StableStateRecord = field(default_factory=StableStateRecord)
    residue: tuple[ResidueRecord, ...] = ()
    runtime: RuntimeProofRecord = field(default_factory=RuntimeProofRecord)
    schema_version: int = REPORT_SCHEMA_VERSION

    @property
    def passed(self) -> bool:
        """Whether the whole real-lab gate passed — not merely one stage of it."""

        return self.status == STATUS_PASS

    def document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
            "code": {"commit": self.code.commit, "dirty": self.code.dirty},
            "profile": {
                "path": self.profile_display,
                "hash": self.profile_hash,
                "state": self.profile_state,
                "name": self.profile_name,
            },
            "lab_identity": self.lab_identity,
            "preflight": [dict(check) for check in self.preflight],
            "baseline": self.baseline.document(),
            "runs": [run.document() for run in self.runs],
            "comparison": self.comparison.document(),
            "stable_state": self.stable_state.document(),
            "residue": [entry.document() for entry in self.residue],
            "runtime": self.runtime.document(),
            "status": self.status,
            "next_action": self.next_action,
        }

    def markdown(self) -> str:
        lines = [
            "# Lab Validation Report",
            "",
            f"- schema version: {self.schema_version}",
            f"- timestamp: {self.timestamp}",
            f"- code: {self.code.display}",
            f"- profile: {self.profile_display}",
            f"- profile hash: {self.profile_hash or 'unknown'}",
            f"- profile state: {self.profile_state or 'unknown'}",
            f"- profile name: {self.profile_name or 'unknown'}",
            "",
            "## Status",
            "",
            f"- status: {self.status}",
            f"- next action: {self.next_action}",
            "",
            "## Lab identity",
            "",
        ]
        lines += _identity_lines(self.lab_identity)
        lines += ["", "## Preflight", ""]
        lines += _table(
            ("check", "result", "detail"),
            [
                (
                    str(check.get("name", "")),
                    "ok" if check.get("ok") else "FAILED",
                    str(check.get("detail", "")),
                )
                for check in self.preflight
            ],
        )
        lines += [
            "",
            "## Preserved baseline",
            "",
            f"- status: {self.baseline.status}",
            f"- report: {self.baseline.report_path or '-'}",
            f"- report hash: {self.baseline.report_hash or '-'}",
            f"- code: {self.baseline.code_commit or '-'}",
            f"- profile hash: {self.baseline.profile_hash or '-'}",
            f"- shell bundle: {self.baseline.shell_bundle_path or '-'}",
            f"- shell bundle hash: {self.baseline.shell_bundle_hash or '-'}",
        ]
        lines += ["", "## Full collect runs", ""]
        lines += _table(
            ("implementation", "exit", "verify", "coverage", "bundle"),
            [
                (
                    run.implementation,
                    "-" if run.exit_code is None else str(run.exit_code),
                    run.verify_result,
                    "complete" if run.coverage.complete else _coverage_gaps(run.coverage),
                    run.bundle_path or "-",
                )
                for run in self.runs
            ],
        )
        lines += [
            "",
            "## Bundle comparison",
            "",
            f"- result: {self.comparison.result}",
        ]
        lines += [f"- difference: {item}" for item in self.comparison.differences]
        lines += [
            "",
            "## Stable state",
            "",
            "- snapshot schema version: "
            + (
                NOT_RUN
                if self.stable_state.schema_version is None
                else str(self.stable_state.schema_version)
            ),
            f"- result: {self.stable_state.result}",
        ]
        lines += [f"- difference: {item}" for item in self.stable_state.differences]
        lines += ["", "## Runtime proof", ""]
        lines += _runtime_lines(self.runtime)
        lines += ["", "## Remote residue", ""]
        lines += _table(
            ("host", "result", "detail"),
            [(entry.host, entry.result, entry.detail) for entry in self.residue],
        )
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ReportLocation:
    """Where one report was written."""

    directory: Path
    json_path: Path
    markdown_path: Path
    latest_path: Path


def write_report(
    runs_directory: Path,
    report: LabValidationReport,
    *,
    directory: Path | None = None,
) -> ReportLocation:
    """Persist one report and point `LATEST` at it, or refuse to write at all.

    `directory` names a run directory the caller already reserved.  The qualification
    gate needs one before it starts — its bundles and command ledgers live there
    — and the report has to land in that same directory rather than a second one
    created at write time.
    """

    _reject_unusable(report)
    document = json.dumps(report.document(), indent=2, sort_keys=True) + "\n"
    markdown = report.markdown()
    # A report is built from bounded diagnostics, but "bounded" is not "safe":
    # scan the finished documents and refuse to persist one that leaks.
    for rendered in (document, markdown):
        marker = _forbidden_marker(rendered)
        if marker is not None:
            raise ReportRejected(
                f"refusing to write a report that carries credential material ({marker})"
            )
    directory = directory or reserve_run_directory(runs_directory)
    json_path = directory / REPORT_JSON_NAME
    markdown_path = directory / REPORT_MARKDOWN_NAME
    write_owner_only(json_path, document)
    write_owner_only(markdown_path, markdown)
    latest_path = runs_directory / LATEST_NAME
    write_owner_only(latest_path, directory.name + "\n")
    return ReportLocation(directory, json_path, markdown_path, latest_path)


def report_from_preflight(
    result: PreflightResult, *, code: CodeIdentity, hosts: tuple[str, ...] = ()
) -> LabValidationReport:
    """Build a report for an identity-preflight-only attempt.

    The collect, comparison, stable-state and residue sections stay `not-run`:
    a passing preflight proves identity, not qualification.
    """

    return LabValidationReport(
        timestamp=utc_timestamp(),
        code=code,
        profile_display=safe_display_path(result.profile_path),
        profile_hash=result.profile.profile_hash,
        profile_state=result.profile.state,
        profile_name=result.profile.name,
        lab_identity=result.identity,
        preflight=tuple(
            {"name": check.name, "ok": check.ok, "detail": check.detail}
            for check in result.checks
        ),
        runs=(RunRecord("python"),),
        residue=tuple(ResidueRecord(host) for host in hosts),
        status=result.status,
        next_action=result.next_action,
    )


def report_from_qualification(result: "QualifyResult", *, code: CodeIdentity) -> LabValidationReport:
    """Build a report for one post-cutover qualification attempt, pass or fail.

    Unlike the preflight report, every section is filled from what the attempt
    actually reached: a run that stopped at the coverage gate still records the
    bundles it produced and leaves the later sections `not-run`, so the report
    says where the gate stopped rather than only that it did.
    """

    return LabValidationReport(
        timestamp=utc_timestamp(),
        code=code,
        profile_display=safe_display_path(result.profile_path),
        profile_hash=result.profile.profile_hash,
        profile_state=result.profile.state,
        profile_name=result.profile.name,
        lab_identity=result.identity,
        preflight=tuple(
            {"name": check.name, "ok": check.ok, "detail": check.detail}
            for check in result.checks
        ),
        baseline=result.baseline,
        runs=result.runs,
        comparison=result.comparison,
        stable_state=result.stable_state,
        residue=result.residue,
        runtime=result.runtime,
        status=result.status,
        next_action=result.next_action,
    )


def report_from_unusable_profile(
    profile_path: Path,
    error: str,
    *,
    code: CodeIdentity,
    status: str,
    next_action: str,
) -> LabValidationReport:
    """Build a report for an attempt that never got past loading the profile.

    A run that could not read its profile is still an attempt, and the runbook
    requires every attempt to leave a report rather than only a terminal message.
    """

    return LabValidationReport(
        timestamp=utc_timestamp(),
        code=code,
        profile_display=safe_display_path(profile_path),
        profile_hash=None,
        profile_state=None,
        profile_name=None,
        preflight=({"name": "profile-load", "ok": False, "detail": error},),
        runs=(RunRecord("python"),),
        status=status,
        next_action=next_action,
    )


def latest_report(runs_directory: Path) -> dict[str, object] | None:
    """Read the most recent report's JSON document, if there is a usable one."""

    pointer = runs_directory / LATEST_NAME
    try:
        name = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or "/" in name or name in (".", ".."):
        return None
    try:
        document = json.loads((runs_directory / name / REPORT_JSON_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    document["directory"] = str(runs_directory / name)
    return document


def _reject_unusable(report: LabValidationReport) -> None:
    if report.schema_version != REPORT_SCHEMA_VERSION:
        raise ReportRejected(
            f"current reports must use schema version {REPORT_SCHEMA_VERSION}"
        )
    if not report.status.strip():
        raise ReportRejected("a report must carry a status")
    action = report.next_action.strip()
    if not action:
        raise ReportRejected("a report must carry exactly one next_action")
    if "\n" in report.next_action:
        raise ReportRejected("a report must carry exactly one next_action, not a list")
    if report.status == STATUS_PASS and not is_python310_qualification(
        report.document()
    ):
        raise ReportRejected(
            "a schema-v3 PASS requires complete runtime proof and qualification evidence"
        )


def is_python310_qualification(document: dict[str, object]) -> bool:
    """Whether an untrusted stored report proves the schema-v3 runtime contract."""

    if (
        document.get("schema_version") != REPORT_SCHEMA_VERSION
        or document.get("status") != STATUS_PASS
        or not runtime_document_is_complete(document.get("runtime"))
    ):
        return False
    baseline = document.get("baseline")
    code = document.get("code")
    comparison = document.get("comparison")
    identity = document.get("lab_identity")
    preflight = document.get("preflight")
    profile = document.get("profile")
    stable = document.get("stable_state")
    runs = document.get("runs")
    residue = document.get("residue")
    if (
        not isinstance(code, dict)
        or re.fullmatch(r"[0-9a-f]{40}", str(code.get("commit", ""))) is None
        or code.get("dirty") is not False
        or not isinstance(profile, dict)
        or profile.get("state") != "active"
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(profile.get("hash", "")))
        is None
        or not isinstance(profile.get("name"), str)
        or not profile["name"]
        or not isinstance(baseline, dict)
        or baseline.get("status") != "pass"
        or not _baseline_is_complete(baseline)
        or baseline.get("profile_hash") != profile.get("hash")
        or not _preflight_is_complete(preflight)
        or not isinstance(comparison, dict)
        or comparison.get("result") != "equivalent"
        or comparison.get("differences") != []
        or not isinstance(stable, dict)
        or stable.get("snapshot_schema_version") != 1
        or stable.get("result") != "unchanged"
        or stable.get("differences") != []
        or not isinstance(runs, list)
        or not isinstance(residue, list)
        or not residue
        or not _identity_is_complete(identity)
    ):
        return False
    if (
        len(runs) != 2
        or [run.get("implementation") for run in runs if isinstance(run, dict)]
        != ["shell", "python"]
        or not all(isinstance(run, dict) and _run_is_complete(run) for run in runs)
        or runs[0].get("bundle_path") != baseline.get("shell_bundle_path")
        or runs[0].get("bundle_hash") != baseline.get("shell_bundle_hash")
    ):
        return False
    verified_hosts = _verified_host_names(identity)
    runtime_hosts = _runtime_host_names(document.get("runtime"))
    residue_hosts = _residue_host_names(residue)
    return (
        verified_hosts is not None
        and verified_hosts == runtime_hosts == residue_hosts
    )


def _baseline_is_complete(baseline: dict[str, object]) -> bool:
    return (
        isinstance(baseline.get("report_path"), str)
        and Path(baseline["report_path"]).is_absolute()
        and isinstance(baseline.get("shell_bundle_path"), str)
        and Path(baseline["shell_bundle_path"]).is_absolute()
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(baseline.get("report_hash", "")))
        is not None
        and re.fullmatch(r"[0-9a-f]{40}", str(baseline.get("code_commit", "")))
        is not None
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(baseline.get("profile_hash", "")))
        is not None
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(baseline.get("shell_bundle_hash", ""))
        )
        is not None
    )


def _preflight_is_complete(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    checks: dict[str, bool] = {}
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "ok", "detail"}
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("detail"), str)
            or item.get("ok") is not True
            or item["name"] in checks
        ):
            return False
        checks[item["name"]] = True
    return REQUIRED_QUALIFICATION_CHECKS <= set(checks)


def _verified_host_names(value: object) -> set[str] | None:
    if not _identity_is_complete(value):
        return None
    assert isinstance(value, dict)
    assert isinstance(value["hosts"], list)
    names = [
        host.get("name")
        for host in value["hosts"]
        if isinstance(host, dict) and isinstance(host.get("name"), str)
    ]
    return set(names) if names and len(names) == len(value["hosts"]) == len(set(names)) else None


def _identity_is_complete(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "ceph_fsid",
        "rook_fsid",
        "prometheus",
        "hosts",
    }:
        return False
    prometheus = value.get("prometheus")
    hosts = value.get("hosts")
    if (
        not isinstance(value.get("ceph_fsid"), str)
        or not value["ceph_fsid"]
        or not isinstance(value.get("rook_fsid"), str)
        or not value["rook_fsid"]
        or not isinstance(prometheus, dict)
        or set(prometheus) != {"url", "ready"}
        or not isinstance(prometheus.get("url"), str)
        or not prometheus["url"]
        or prometheus.get("ready") is not True
        or not isinstance(hosts, list)
        or not hosts
    ):
        return False
    for host in hosts:
        if not isinstance(host, dict) or set(host) != {
            "name",
            "address",
            "hostname",
            "ssh_fingerprints_verified",
        }:
            return False
        fingerprints = host.get("ssh_fingerprints_verified")
        if (
            not all(
                isinstance(host.get(field), str) and host[field]
                for field in ("name", "address", "hostname")
            )
            or not isinstance(fingerprints, list)
            or not fingerprints
            or not all(isinstance(item, str) and item for item in fingerprints)
            or len(fingerprints) != len(set(fingerprints))
        ):
            return False
    names = [host["name"] for host in hosts]
    return len(names) == len(set(names))


def _runtime_host_names(value: object) -> set[str] | None:
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), dict):
        return None
    pre = _probe_documents(value["nodes"].get("pre"))
    return None if pre is None else set(pre)


def _residue_host_names(value: list[object]) -> set[str] | None:
    names: list[str] = []
    for entry in value:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"host", "result", "detail"}
            or not isinstance(entry.get("host"), str)
            or not entry["host"]
            or entry.get("result") != "clean"
            or not isinstance(entry.get("detail"), str)
        ):
            return None
        names.append(entry["host"])
    return set(names) if len(names) == len(set(names)) else None


def _run_is_complete(run: dict[str, object]) -> bool:
    coverage = run.get("coverage")
    return (
        run.get("exit_code") == 0
        and isinstance(run.get("invocation_id"), str)
        and bool(run["invocation_id"])
        and isinstance(run.get("bundle_path"), str)
        and Path(run["bundle_path"]).is_absolute()
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(run.get("bundle_hash", "")))
        is not None
        and run.get("verify_result") == "pass"
        and isinstance(coverage, dict)
        and set(coverage) == set(COLLECTOR_PATHS)
        and all(value == "collected" for value in coverage.values())
    )


def runtime_document_is_complete(value: object) -> bool:
    """Validate the machine-readable runtime proof without trusting its producer."""

    if not isinstance(value, dict):
        return False
    tooling = value.get("tooling")
    production = value.get("production")
    if not _runtime_matches(tooling, minimum=(3, 11)):
        return False
    if not _runtime_matches(production, exact=(3, 10), implementation="cpython"):
        return False
    witness = value.get("floor_witness")
    comparison = value.get("comparison")
    nodes = value.get("nodes")
    if (
        not isinstance(witness, str)
        or not witness
        or not isinstance(comparison, dict)
        or comparison.get("result") != "unchanged"
        or comparison.get("differences") != []
        or not isinstance(nodes, dict)
    ):
        return False
    pre = _probe_documents(nodes.get("pre"))
    post = _probe_documents(nodes.get("post"))
    if pre is None or post is None or pre != post or witness not in pre:
        return False
    if not all(
        _runtime_matches(probe.get("runtime"), minimum=(3, 10), implementation="cpython")
        for probe in pre.values()
    ):
        return False
    return _runtime_matches(
        pre[witness].get("runtime"), exact=(3, 10), implementation="cpython"
    )


def _probe_documents(value: object) -> dict[str, dict[str, object]] | None:
    if not isinstance(value, dict) or not isinstance(value.get("probes"), list):
        return None
    probes: dict[str, dict[str, object]] = {}
    for item in value["probes"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"host", "exit_code", "status", "runtime", "detail"}
            or not isinstance(item.get("host"), str)
            or not item["host"]
            or item.get("status") != "ok"
            or item.get("exit_code") != 0
            or not isinstance(item.get("detail"), str)
            or item["host"] in probes
        ):
            return None
        probes[item["host"]] = item
    return probes or None


def _runtime_matches(
    value: object,
    *,
    minimum: tuple[int, int] | None = None,
    exact: tuple[int, int] | None = None,
    implementation: str | None = None,
) -> bool:
    try:
        runtime = parse_runtime_identity(json.dumps(value))
    except (TypeError, ValueError):
        return False
    if implementation is not None and runtime.implementation != implementation:
        return False
    pair = (runtime.version_info.major, runtime.version_info.minor)
    return (minimum is None or pair >= minimum) and (exact is None or pair == exact)


def _forbidden_marker(text: str) -> str | None:
    for marker in CREDENTIAL_MARKERS:
        if marker in text:
            return marker
    return None


def reserve_run_directory(runs_directory: Path) -> Path:
    """Create a fresh run directory, without colliding with an existing run."""

    runs_directory.mkdir(parents=True, exist_ok=True)
    base = datetime.now(timezone.utc).strftime(RUN_ID_FORMAT)
    candidate = runs_directory / base
    attempt = 1
    while True:
        try:
            candidate.mkdir(mode=0o700)
            return candidate
        except FileExistsError:
            attempt += 1
            candidate = runs_directory / f"{base}-{attempt}"


def _identity_lines(identity: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for key in ("ceph_fsid", "rook_fsid"):
        if key in identity:
            lines.append(f"- {key.replace('_', ' ')}: {identity[key]}")
    prometheus = identity.get("prometheus")
    if isinstance(prometheus, dict):
        lines.append(
            f"- prometheus: {prometheus.get('url')} "
            f"(ready: {'yes' if prometheus.get('ready') else 'no'})"
        )
    hosts = identity.get("hosts")
    if isinstance(hosts, list) and hosts:
        lines += [""]
        lines += _table(
            ("host", "address", "hostname", "verified host keys"),
            [
                (
                    str(host.get("name", "")),
                    str(host.get("address", "")),
                    str(host.get("hostname") or "-"),
                    ", ".join(host.get("ssh_fingerprints_verified", [])) or "-",
                )
                for host in hosts
                if isinstance(host, dict)
            ],
        )
    return lines or ["- no lab identity was verified"]


def _coverage_gaps(coverage: CollectorCoverage) -> str:
    return ", ".join(coverage.gaps())


def _runtime_lines(runtime: RuntimeProofRecord) -> list[str]:
    tooling = runtime.tooling.document() if runtime.tooling is not None else None
    production = (
        runtime.production.document() if runtime.production is not None else None
    )
    lines = [
        "- tooling: " + (json.dumps(tooling, sort_keys=True) if tooling else NOT_RUN),
        "- production: "
        + (json.dumps(production, sort_keys=True) if production else NOT_RUN),
        f"- comparison: {runtime.comparison_result}",
        f"- floor witness: {runtime.floor_witness or NOT_RUN}",
    ]
    lines += [f"- difference: {item}" for item in runtime.differences]
    for moment, snapshot in (("pre", runtime.nodes_pre), ("post", runtime.nodes_post)):
        if snapshot is None:
            lines.append(f"- node probes {moment}: {NOT_RUN}")
            continue
        for probe in snapshot.probes:
            identity = probe.runtime.document() if probe.runtime is not None else None
            lines.append(
                f"- node probe {moment} {probe.host}: {probe.status} "
                f"(exit {probe.exit_code}, runtime "
                f"{json.dumps(identity, sort_keys=True) if identity else '-'})"
            )
    return lines


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    if not rows:
        return ["- none recorded"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    return lines
