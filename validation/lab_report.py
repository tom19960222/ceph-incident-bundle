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

The identity, preflight and status fields are filled today.  Collector coverage,
the two full-collect runs, the normalized bundle comparison, the stable-state
diff and the per-node residue checks are declared here so the schema is fixed,
and stay `not-run` until the dual-run harness (issue #20) fills them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from validation.lab_local import CodeIdentity, utc_run_id, utc_timestamp, write_owner_only
from validation.lab_preflight import PreflightResult
from validation.lab_profile import safe_display_path


REPORT_SCHEMA_VERSION = 1
REPORT_JSON_NAME = "report.json"
REPORT_MARKDOWN_NAME = "report.md"
LATEST_NAME = "LATEST"
NOT_RUN = "not-run"
COLLECTOR_PATHS = ("ceph", "rook", "prometheus", "nodes", "var_log")
# A report is written from bounded diagnostics, but "bounded" is not "safe": scan
# the finished documents and refuse to persist one that carries credentials.
FORBIDDEN_MARKERS = (
    "-----BEGIN",
    "PRIVATE KEY",
    "Authorization:",
    "client-key-data",
    "client-certificate-data",
)


class ReportRejected(Exception):
    """A report was refused rather than written in an unsafe or ambiguous shape."""


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
        return all(getattr(self, path) == "collected" for path in COLLECTOR_PATHS)


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
    profile_hash: str
    profile_state: str
    profile_name: str
    status: str
    next_action: str
    lab_identity: dict[str, object] = field(default_factory=dict)
    preflight: tuple[dict[str, object], ...] = ()
    runs: tuple[RunRecord, ...] = ()
    comparison: ComparisonRecord = field(default_factory=ComparisonRecord)
    stable_state: StableStateRecord = field(default_factory=StableStateRecord)
    residue: tuple[ResidueRecord, ...] = ()
    schema_version: int = REPORT_SCHEMA_VERSION

    @property
    def passed(self) -> bool:
        return self.status == "pass"

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
            "runs": [run.document() for run in self.runs],
            "comparison": self.comparison.document(),
            "stable_state": self.stable_state.document(),
            "residue": [entry.document() for entry in self.residue],
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
            f"- profile hash: {self.profile_hash}",
            f"- profile state: {self.profile_state}",
            f"- profile name: {self.profile_name}",
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
        lines += ["", "## Full collect runs", ""]
        lines += _table(
            ("implementation", "exit", "verify", "coverage", "bundle"),
            [
                (
                    run.implementation,
                    str(run.exit_code),
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
            f"- snapshot schema version: {self.stable_state.schema_version}",
            f"- result: {self.stable_state.result}",
        ]
        lines += [f"- difference: {item}" for item in self.stable_state.differences]
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


def write_report(runs_directory: Path, report: LabValidationReport) -> ReportLocation:
    """Persist one report and point `LATEST` at it, or refuse to write at all."""

    _reject_unusable(report)
    document = json.dumps(report.document(), indent=2, sort_keys=True) + "\n"
    markdown = report.markdown()
    for rendered in (document, markdown):
        marker = _forbidden_marker(rendered)
        if marker is not None:
            raise ReportRejected(
                f"refusing to write a report that carries credential material ({marker})"
            )
    directory = _reserve_run_directory(runs_directory)
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
        runs=(RunRecord("shell"), RunRecord("python")),
        residue=tuple(ResidueRecord(host) for host in hosts),
        status=result.status,
        next_action=result.next_action,
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
    if not report.status.strip():
        raise ReportRejected("a report must carry a status")
    action = report.next_action.strip()
    if not action:
        raise ReportRejected("a report must carry exactly one next_action")
    if "\n" in report.next_action:
        raise ReportRejected("a report must carry exactly one next_action, not a list")


def _forbidden_marker(text: str) -> str | None:
    for marker in FORBIDDEN_MARKERS:
        if marker in text:
            return marker
    return None


def _reserve_run_directory(runs_directory: Path) -> Path:
    """Create a fresh run directory, without colliding with an existing run."""

    runs_directory.mkdir(parents=True, exist_ok=True)
    base = utc_run_id()
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
    return ", ".join(
        f"{path}={getattr(coverage, path)}"
        for path in COLLECTOR_PATHS
        if getattr(coverage, path) != "collected"
    )


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
