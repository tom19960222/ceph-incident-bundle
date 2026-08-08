"""Validate the preserved pre-cutover evidence used by the final lab gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from validation.lab_bundle import BundleContents, BundleUnreadable, read_bundle
from validation.lab_profile import LabProfile
from validation.lab_report import (
    COLLECTOR_PATHS,
    REPORT_JSON_NAME,
    BaselineRecord,
    CollectorCoverage,
    RunRecord,
)


class BaselineRejected(Exception):
    """The selected report cannot support a post-cutover comparison."""


@dataclass(frozen=True)
class CutoverBaseline:
    report_path: Path
    report_hash: str
    code_commit: str
    profile_hash: str
    identity: dict[str, object]
    shell_bundle_path: Path
    shell_bundle_hash: str
    shell_contents: BundleContents
    shell_run: RunRecord

    def record(self) -> BaselineRecord:
        return BaselineRecord(
            status="pass",
            report_path=str(self.report_path),
            report_hash=self.report_hash,
            code_commit=self.code_commit,
            profile_hash=self.profile_hash,
            shell_bundle_path=str(self.shell_bundle_path),
            shell_bundle_hash=self.shell_bundle_hash,
        )


def load_cutover_baseline(report: Path, *, profile: LabProfile) -> CutoverBaseline:
    """Load one #21 PASS report and prove its shell bundle is still the same bytes."""

    report_path = report / REPORT_JSON_NAME if report.is_dir() else report
    try:
        raw = report_path.read_bytes()
        document = json.loads(raw)
    except (OSError, ValueError) as error:
        raise BaselineRejected(f"cannot read baseline report {report_path}: {error}") from error
    if not isinstance(document, dict):
        raise BaselineRejected("baseline report must be a JSON object")
    if document.get("schema_version") != 1:
        raise BaselineRejected("baseline report must use qualification schema version 1")
    if document.get("status") != "pass":
        raise BaselineRejected("baseline report status is not pass")

    code = _mapping(document, "code")
    commit = code.get("commit")
    if not isinstance(commit, str) or len(commit) != 40 or code.get("dirty") is not False:
        raise BaselineRejected("baseline report does not name one clean commit")

    recorded_profile = _mapping(document, "profile")
    profile_hash = recorded_profile.get("hash")
    if profile_hash != profile.profile_hash:
        raise BaselineRejected(
            "baseline profile hash does not match the active post-cutover profile"
        )

    comparison = _mapping(document, "comparison")
    if comparison.get("result") != "equivalent" or comparison.get("differences") != []:
        raise BaselineRejected("baseline shell/Python comparison was not equivalent")
    stable_state = _mapping(document, "stable_state")
    if stable_state.get("result") != "unchanged" or stable_state.get("differences") != []:
        raise BaselineRejected("baseline stable-state proof was not unchanged")
    residue = document.get("residue")
    if not isinstance(residue, list) or not residue or any(
        not isinstance(item, dict) or item.get("result") != "clean" for item in residue
    ):
        raise BaselineRejected("baseline remote-residue proof was not clean")

    runs = document.get("runs")
    if not isinstance(runs, list):
        raise BaselineRejected("baseline report has no run records")
    shell_runs = [
        item for item in runs if isinstance(item, dict) and item.get("implementation") == "shell"
    ]
    if len(shell_runs) != 1:
        raise BaselineRejected("baseline report must name exactly one shell run")
    shell = shell_runs[0]
    coverage = shell.get("coverage")
    if (
        shell.get("exit_code") != 0
        or shell.get("verify_result") != "pass"
        or not isinstance(coverage, dict)
        or any(coverage.get(name) != "collected" for name in COLLECTOR_PATHS)
    ):
        raise BaselineRejected("baseline shell run was not verified with complete coverage")
    bundle_name = shell.get("bundle_path")
    bundle_hash = shell.get("bundle_hash")
    if not isinstance(bundle_name, str) or not isinstance(bundle_hash, str):
        raise BaselineRejected("baseline shell run does not identify its bundle")
    bundle_path = Path(bundle_name)
    actual_hash = _sha256(bundle_path)
    if actual_hash != bundle_hash:
        raise BaselineRejected("baseline shell bundle hash no longer matches the PASS report")
    try:
        contents = read_bundle(bundle_path)
    except BundleUnreadable as error:
        raise BaselineRejected(f"baseline shell bundle is unreadable: {error}") from error

    identity = document.get("lab_identity")
    if not isinstance(identity, dict) or not identity:
        raise BaselineRejected("baseline report has no verified lab identity")
    return CutoverBaseline(
        report_path=report_path,
        report_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
        code_commit=commit,
        profile_hash=profile_hash,
        identity=identity,
        shell_bundle_path=bundle_path,
        shell_bundle_hash=bundle_hash,
        shell_contents=contents,
        shell_run=RunRecord(
            "shell",
            exit_code=0,
            invocation_id=(
                shell.get("invocation_id")
                if isinstance(shell.get("invocation_id"), str)
                else bundle_path.name
            ),
            bundle_path=str(bundle_path),
            bundle_hash=bundle_hash,
            verify_result="pass",
            coverage=CollectorCoverage(
                *(str(coverage[name]) for name in COLLECTOR_PATHS)
            ),
        ),
    )


def _mapping(document: dict[str, object], name: str) -> dict[str, object]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise BaselineRejected(f"baseline report field {name} must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise BaselineRejected(f"cannot read baseline shell bundle {path}: {error}") from error
    return f"sha256:{digest.hexdigest()}"
