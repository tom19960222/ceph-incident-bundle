"""`validate-lab`: the post-cutover real-lab qualification gate.

This is the harness the runbook's Qualification Workflow describes.  From one
explicitly activated Lab Profile it derives a single shared inventory, proves the
lab's identity, validates the preserved #21 shell baseline, snapshots the lab's
stable state, runs one Python *full* collect, and compares its normalized contract
with the preserved bundle.

Three properties are structural rather than advisory:

- **Order.** Every stage runs after the one that makes its answer meaningful.  A
  bundle collected before identity was proven is evidence about an unknown
  machine; a residue check with no pre-run baseline cannot attribute what it
  finds.  The first failing stage stops the run — with one exception, below.
- **Nothing is waivable.** There is no skip flag, no "accept current", and no
  path that reruns a stage until it passes.  Partial coverage, a failed verify, a
  contract difference, a stable-state change and remote residue are each a
  failure with one next action.
- **Read-only stays read-only.** The live invocation's argv comes from one builder
  and is checked for `--allow-cephadm-shell` and `--allow-kubectl-exec` before
  anything runs; every `CEPH_INCIDENT_*` variable is stripped from the
  environment they inherit, because the collectors read some of those as *flag
  defaults*; and host key trust is pinned to the keys the active profile already
  records rather than delegated to the collector's accept-new mode.

The exception to "stop at the first failure" is the residue check: once a collect
has started, the lab has been touched, and the runs where something went wrong
are the ones most likely to have leaked.  So it runs even after an earlier stage
failed, and a residue finding replaces the earlier failure class — a lab left
dirty is what has to reach a person first.

The collect and verify entrypoints, and the checkout whose commit the report
names, are injectable so the harness itself can be exercised offline against a
fake lab; production always uses the real ones.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from validation.lab_baseline import BaselineRejected, CutoverBaseline, load_cutover_baseline
from validation.lab_bundle import (
    BundleContents,
    BundleUnreadable,
    contract_of,
    coverage_of,
    read_bundle,
)
from validation.lab_commands import CUTOVER_TICKET, qualify_command
from validation.lab_contract import describe_differences
from validation.lab_inventory import write_inventory, write_known_hosts_home
from validation.lab_output import write_owner_only
from validation.lab_preflight import PreflightCheck, preflight
from validation.lab_probe import (
    EXIT_COMMAND_MISSING,
    EXIT_TIMEOUT,
    LabProber,
    bounded_diagnostic,
)
from validation.lab_profile import LabProfile, load_profile, safe_display_path
from validation.lab_report import (
    BaselineRecord,
    ComparisonRecord,
    ResidueRecord,
    RunRecord,
    StableStateRecord,
    code_identity,
    tracked_modifications,
)
from validation.lab_residue import (
    RESULT_CLEAN,
    RESULT_UNREADABLE,
    ResidueListing,
    ResidueUnavailable,
    compare_residue,
    observe_residue,
)
from validation.lab_snapshot import (
    RESULT_CHANGED,
    RESULT_UNCHANGED,
    SnapshotUnavailable,
    capture_stable_state,
    compare_snapshots,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

STATUS_PASS = "pass"
FAILURE_CODE_IDENTITY = "code-identity-unclear"
FAILURE_OPT_IN = "side-effecting-opt-in-enabled"
FAILURE_STABLE_STATE_UNREADABLE = "stable-state-unreadable"
FAILURE_RESIDUE_UNREADABLE = "residue-baseline-unreadable"
FAILURE_COLLECT = "collect-failed"
FAILURE_BUNDLE = "bundle-unreadable"
FAILURE_VERIFY = "verify-failed"
FAILURE_COVERAGE = "coverage-incomplete"
FAILURE_COMPARISON = "bundle-comparison-failed"
FAILURE_STABLE_STATE = "stable-state-changed"
FAILURE_RESIDUE = "remote-residue"
FAILURE_BASELINE = "baseline-invalid"
FAILURE_WORKSTATION_RESIDUE = "workstation-residue"

# The qualification collect vector.  It is a constant, not an operator input:
# the gate's claim is about *this* invocation shape, and the two side-effecting
# opt-ins (`--allow-cephadm-shell`, `--allow-kubectl-exec`) are absent by
# construction rather than by remembering to leave them out.
QUALIFICATION_ARGUMENTS = (
    "--mode",
    "auto",
    "--kube-mode",
    "local",
    "--since",
    "24h",
    # Host key trust is the profile's, pinned through the collector-owned HOME
    # built below; the collector's own accept-new mode is never used here.
    "--no-trust-ssh-host-key",
    "--redact",
)
FORBIDDEN_ARGUMENTS = ("--allow-cephadm-shell", "--allow-kubectl-exec")
# Both collectors take defaults and safety-limit overrides from variables under
# this prefix, so the whole prefix is cleared before either one runs.
COLLECTOR_VARIABLE_PREFIX = "CEPH_INCIDENT_"
# A full collect of a real lab reads every node's /var/log, so the ceiling is
# there to catch a stuck run, not a slow one.  `--collect-timeout` moves it
# because lab sizes differ by orders of magnitude; unlike the argv above it is
# not part of what the collect is *asked to do*, so tuning it cannot change what
# the gate proves — only how long it waits before calling a run stuck.
DEFAULT_COLLECT_TIMEOUT_SECONDS = 4 * 60 * 60
VERIFY_TIMEOUT_SECONDS = 30 * 60
BUNDLE_GLOB = "ceph-incident-*.tar.gz"


@dataclass(frozen=True)
class CollectEntrypoint:
    """One implementation's collect and verify entrypoints."""

    implementation: str
    collect: tuple[str, ...]
    verify: tuple[str, ...]


def default_entrypoints() -> tuple[CollectEntrypoint, ...]:
    """The only production implementation after the cutover."""

    return (
        CollectEntrypoint(
            "python",
            (sys.executable, str(REPOSITORY_ROOT / "ceph_incident_bundle.py"), "collect"),
            (sys.executable, str(REPOSITORY_ROOT / "ceph_incident_bundle.py"), "verify"),
        ),
    )


def collect_arguments(
    profile: LabProfile, *, inventory: Path, output_root: Path
) -> list[str]:
    """The argv one qualification collect is given, minus its entrypoint.

    Both invocations are built here so they cannot drift, and so the read-only
    opt-in check can inspect exactly what will be run.
    """

    return [
        "--inventory",
        str(inventory),
        "--ssh-key",
        str(profile.ssh_key_path),
        "--out",
        str(output_root),
        "--prom-url",
        profile.prometheus_url,
        *QUALIFICATION_ARGUMENTS,
    ]


@dataclass(frozen=True)
class QualifyResult:
    """One qualification attempt's verdict, in the shape a report records."""

    profile_path: Path
    profile: LabProfile
    status: str
    next_action: str
    run_directory: Path
    checks: tuple[PreflightCheck, ...] = ()
    identity: dict[str, object] = field(default_factory=dict)
    baseline: BaselineRecord = field(default_factory=BaselineRecord)
    runs: tuple[RunRecord, ...] = ()
    comparison: ComparisonRecord = field(default_factory=ComparisonRecord)
    stable_state: StableStateRecord = field(default_factory=StableStateRecord)
    residue: tuple[ResidueRecord, ...] = ()
    blocked_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_PASS

    def summary(self) -> dict[str, object]:
        return {
            "profile": safe_display_path(self.profile_path),
            "profile_hash": self.profile.profile_hash,
            "profile_state": self.profile.state,
            "run_directory": str(self.run_directory),
            "checks": [
                {"name": check.name, "ok": check.ok, "detail": check.detail}
                for check in self.checks
            ],
            "verified_identity": self.identity,
            "baseline": self.baseline.document(),
            "runs": [run.document() for run in self.runs],
            "comparison": self.comparison.document(),
            "stable_state": self.stable_state.document(),
            "residue": [entry.document() for entry in self.residue],
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "next_action": self.next_action,
        }

    def text(self) -> str:
        lines = ["Real-lab qualification"]
        for check in self.checks:
            lines.append(
                f"  {check.name}: {'ok' if check.ok else 'FAILED'} ({check.detail})"
            )
        for run in self.runs:
            lines.append(
                f"  run {run.implementation}: exit "
                f"{'-' if run.exit_code is None else run.exit_code}, verify "
                f"{run.verify_result}, coverage "
                f"{'complete' if run.coverage.complete else 'incomplete'}"
            )
        lines += [
            f"  comparison: {self.comparison.result}",
            f"  stable state: {self.stable_state.result}",
        ]
        for entry in self.residue:
            lines.append(f"  residue {entry.host}: {entry.result} ({entry.detail})")
        lines += [
            f"  status:    {self.status}",
            f"  report:    {self.run_directory}",
        ]
        if self.blocked_reason:
            lines.append(f"  blocked by: {self.blocked_reason}")
        lines += ["", f"next action: {self.next_action}"]
        return "\n".join(lines) + "\n"


def qualify(
    profile_path: Path,
    *,
    run_directory: Path,
    baseline_report: Path | None = None,
    entrypoints: Sequence[CollectEntrypoint] | None = None,
    collect_timeout: int = DEFAULT_COLLECT_TIMEOUT_SECONDS,
    repository_root: Path = REPOSITORY_ROOT,
) -> QualifyResult:
    """Run the whole real-lab gate, stopping at the first stage that fails.

    `repository_root` is the checkout whose commit the report will name.  It is a
    parameter for the same reason the entrypoints are: the harness's own tests
    drive it against a checkout they control, and a gate that could only be
    exercised from a pristine copy of this repository would not be exercised.
    """

    profile = load_profile(profile_path)
    run = _Qualification(
        profile_path,
        profile,
        run_directory=run_directory,
        baseline_report=baseline_report,
        entrypoints=tuple(
            entrypoints
            if entrypoints is not None
            else default_entrypoints()
        ),
        collect_timeout=collect_timeout,
        repository_root=repository_root,
    )
    return run.execute()


@dataclass
class _Collected:
    """What one implementation's full collect produced, before comparison."""

    record: RunRecord
    contents: BundleContents


class _Qualification:
    """One qualification run: ordered stages, accumulated evidence, one verdict."""

    def __init__(
        self,
        profile_path: Path,
        profile: LabProfile,
        *,
        run_directory: Path,
        baseline_report: Path | None,
        entrypoints: tuple[CollectEntrypoint, ...],
        collect_timeout: int,
        repository_root: Path,
    ) -> None:
        self.profile_path = profile_path
        self.profile = profile
        self.run_directory = run_directory
        self.baseline_report = baseline_report
        self.entrypoints = entrypoints
        self.collect_timeout = collect_timeout
        self.repository_root = repository_root
        self.checks: list[PreflightCheck] = []
        self.identity: dict[str, object] = {}
        self.runs: list[RunRecord] = []
        self.residue: list[ResidueRecord] = []
        # Set the moment a collect entrypoint is launched, not when it produces a
        # usable run record: a collect that failed part-way still reached the
        # nodes, so it still owes them a residue check.
        self.collect_started = False
        self.comparison = ComparisonRecord()
        self.stable_state = StableStateRecord()
        self.node_invocation_ids: tuple[str, ...] = ()
        self.baseline: CutoverBaseline | None = None

    # -- stage plumbing ----------------------------------------------------

    def _passed(self, name: str, detail: str) -> None:
        self.checks.append(PreflightCheck(name=name, ok=True, detail=detail))

    def _failed(
        self, name: str, detail: str, failure_class: str, next_action: str
    ) -> QualifyResult:
        self.checks.append(PreflightCheck(name=name, ok=False, detail=detail))
        return self._result(failure_class, next_action, blocked_reason=detail)

    def _result(
        self, status: str, next_action: str, *, blocked_reason: str | None = None
    ) -> QualifyResult:
        return QualifyResult(
            profile_path=self.profile_path,
            profile=self.profile,
            status=status,
            next_action=next_action,
            run_directory=self.run_directory,
            checks=tuple(self.checks),
            identity=self.identity,
            baseline=(
                self.baseline.record() if self.baseline is not None else BaselineRecord()
            ),
            runs=self._run_records(),
            comparison=self.comparison,
            stable_state=self.stable_state,
            residue=tuple(self.residue)
            or tuple(ResidueRecord(host.name) for host in self.profile.hosts),
            blocked_reason=blocked_reason,
        )

    def _run_records(self) -> tuple[RunRecord, ...]:
        """Both implementations, always — the one that never ran stays `not-run`.

        A report that simply omits the candidate would read as though only the
        reference was ever in scope; the schema's job is to say which of the two
        got how far.
        """

        recorded = {run.implementation: run for run in self.runs}
        implementations = (
            ("shell", *(entrypoint.implementation for entrypoint in self.entrypoints))
            if self.baseline is not None
            else tuple(entrypoint.implementation for entrypoint in self.entrypoints)
        )
        return tuple(
            recorded.get(implementation, RunRecord(implementation))
            for implementation in implementations
        )

    # -- the gate ----------------------------------------------------------

    def execute(self) -> QualifyResult:
        code = self._check_code_identity()
        if code is not None:
            return code
        opt_ins = self._check_read_only_opt_ins()
        if opt_ins is not None:
            return opt_ins
        if self.baseline_report is not None:
            try:
                self.baseline = load_cutover_baseline(
                    self.baseline_report, profile=self.profile
                )
            except BaselineRejected as error:
                return self._failed(
                    "baseline-evidence",
                    str(error),
                    FAILURE_BASELINE,
                    "Select the preserved #21 PASS report and matching shell bundle "
                    "before re-running the post-cutover gate",
                )
            self.runs.append(self.baseline.shell_run)
            self._passed(
                "baseline-evidence",
                f"PASS report {self.baseline.report_path} at code "
                f"{self.baseline.code_commit} still verifies shell bundle "
                f"{self.baseline.shell_bundle_hash}",
            )
        # The identity preflight owns profile state, credential paths, host keys
        # and cluster identity; running its stages here too would give a report
        # two verdicts on the same question.
        identity = preflight(self.profile_path)
        self.checks += list(identity.checks)
        self.identity = dict(identity.identity)
        if not identity.ok:
            return self._result(
                identity.status,
                identity.next_action,
                blocked_reason=identity.blocked_reason,
            )
        if self.baseline is not None and self.identity != self.baseline.identity:
            return self._failed(
                "baseline-identity",
                "the currently verified lab identity differs from the preserved baseline",
                FAILURE_BASELINE,
                "Review or replace the Lab Profile; post-cutover proof must use the "
                "same verified lab as the preserved shell baseline",
            )
        if self.baseline is not None:
            self._passed(
                "baseline-identity",
                "the active profile resolves to the same Ceph, Rook, Prometheus and "
                "host identity as the preserved baseline",
            )
        with tempfile.TemporaryDirectory(
            prefix="ceph-incident-lab-qualify."
        ) as workspace:
            return self._collect_and_compare(Path(workspace), identity.trusted_host_keys)

    def _check_code_identity(self) -> QualifyResult | None:
        """Require the qualification to name the code it actually ran.

        A report says "these two implementations agreed" and points at a commit.
        If tracked files were modified, that commit does not describe what ran,
        and the claim cannot be reproduced or reviewed — so the gate stops rather
        than producing evidence about code nobody can look up.  Untracked files
        are not a problem: a local-only Lab Profile beside the repository is the
        normal case.
        """

        modified = tracked_modifications(self.repository_root)
        if modified:
            listed = ", ".join(modified[:5]) + (
                f" and {len(modified) - 5} more" if len(modified) > 5 else ""
            )
            return self._failed(
                "code-identity",
                f"{len(modified)} tracked file(s) differ from HEAD: {listed}",
                FAILURE_CODE_IDENTITY,
                "Commit or stash the modified tracked files so the report names the "
                "code that ran, then re-run "
                f"{qualify_command(self.profile_path, self.baseline_report)}",
            )
        commit = code_identity(self.repository_root).commit
        self._passed("code-identity", f"running commit {commit} with no local changes")
        return None

    def _check_read_only_opt_ins(self) -> QualifyResult | None:
        """Prove this run cannot ask for a side-effecting collector path.

        There are two ways in, so both are checked.  The argv the collects will
        actually be given — the fixed vector *and* the profile-derived words, not
        the constant alone — and the environment they will inherit, because the
        collectors read some opt-ins as defaults from `CEPH_INCIDENT_*`.
        """

        words = collect_arguments(
            self.profile, inventory=Path("<inventory>"), output_root=Path("<out>")
        )
        enabled = [argument for argument in FORBIDDEN_ARGUMENTS if argument in words]
        if enabled:
            return self._failed(
                "read-only-opt-ins",
                f"the qualification collect argv enables {', '.join(enabled)}",
                FAILURE_OPT_IN,
                "Remove the side-effecting opt-in from collect_arguments() in "
                "validation/lab_qualify.py before running the gate again",
            )
        inherited = sorted(
            name
            for name in self._environment(Path("<home>"))
            if name.startswith(COLLECTOR_VARIABLE_PREFIX)
        )
        if inherited:
            return self._failed(
                "read-only-opt-ins",
                f"the collect environment still carries {', '.join(inherited)}",
                FAILURE_OPT_IN,
                "Stop passing collector variables through _environment() in "
                "validation/lab_qualify.py before running the gate again",
            )
        self._passed(
            "read-only-opt-ins",
            "cephadm shell and kubectl exec stay disabled for the live invocation, in "
            "the argv and in the inherited environment",
        )
        return None

    def _collect_and_compare(
        self, workspace: Path, trusted_host_keys: tuple[str, ...]
    ) -> QualifyResult:
        inventory = write_inventory(self.profile, workspace)
        home = workspace / "home"
        home.mkdir(mode=0o700)
        write_known_hosts_home(home, trusted_host_keys)
        self._passed(
            "shared-inventory",
            f"one inventory for the post-cutover invocation: {len(self.profile.hosts)} host(s), "
            f"seed {self.profile.ceph_seed}, host keys pinned from the active profile",
        )

        prober = LabProber(self.profile, workspace=workspace)
        known_hosts = workspace / "probe-known-hosts"
        write_owner_only(known_hosts, "".join(f"{line}\n" for line in trusted_host_keys))

        try:
            before = capture_stable_state(prober, self.profile, known_hosts)
        except SnapshotUnavailable as error:
            return self._snapshot_unreadable("stable-state-pre", error)
        self._passed(
            "stable-state-pre",
            f"schema {before.schema_version} snapshot of {len(before.fields)} stable source(s)",
        )

        try:
            baseline = {
                host.name: observe_residue(prober, host, known_hosts)
                for host in self.profile.hosts
            }
        except ResidueUnavailable as error:
            return self._failed(
                "residue-baseline",
                str(error),
                FAILURE_RESIDUE_UNREADABLE,
                f"Restore read-only SSH access to every inventory node ({error}) and "
                f"re-run {qualify_command(self.profile_path, self.baseline_report)}",
            )
        self._passed(
            "residue-baseline",
            f"pre-collection workspace listing taken on {len(baseline)} node(s)",
        )

        collected: list[_Collected] = (
            [_Collected(self.baseline.shell_run, self.baseline.shell_contents)]
            if self.baseline is not None
            else []
        )
        for entrypoint in self.entrypoints:
            outcome = self._collect(entrypoint, inventory, home)
            if isinstance(outcome, QualifyResult):
                return self._finish(outcome, prober, known_hosts, baseline)
            collected.append(outcome)
            self.runs.append(outcome.record)
            # Coverage is gated here rather than after both runs: once one
            # invocation is known to have missed a collector path the gate has
            # already failed, and a second full collect would touch every node in
            # the lab again to prove something the report can already say.
            coverage_failure = self._check_coverage(outcome.record)
            if coverage_failure is not None:
                return self._finish(coverage_failure, prober, known_hosts, baseline)
            cleanup_failure = self._check_workstation_cleanup(outcome.record)
            if cleanup_failure is not None:
                return self._finish(cleanup_failure, prober, known_hosts, baseline)

        comparison_failure = self._compare(collected)
        if comparison_failure is not None:
            return self._finish(comparison_failure, prober, known_hosts, baseline)

        try:
            after = capture_stable_state(prober, self.profile, known_hosts)
        except SnapshotUnavailable as error:
            return self._finish(
                self._snapshot_unreadable("stable-state-post", error),
                prober,
                known_hosts,
                baseline,
            )
        result, schema, differences = compare_snapshots(before, after)
        self.stable_state = StableStateRecord(
            schema_version=before.schema_version, result=result, differences=differences
        )
        if result == RESULT_CHANGED:
            return self._finish(
                self._failed(
                    "stable-state-post",
                    f"{len(differences)} stable field(s) changed across the two collects",
                    FAILURE_STABLE_STATE,
                    f"Review the changed stable fields in {self.run_directory} against "
                    "the two command ledgers before any cutover — collection must not "
                    "change persistent state",
                ),
                prober,
                known_hosts,
                baseline,
            )
        self._passed("stable-state-post", f"schema {schema} snapshot is {RESULT_UNCHANGED}")

        residue = self._check_residue(prober, known_hosts, baseline)
        if residue is not None:
            return residue
        return self._result(
            STATUS_PASS,
            f"Hand off {self.run_directory} to {CUTOVER_TICKET}",
        )

    # -- one implementation's full collect ---------------------------------

    def _collect(
        self, entrypoint: CollectEntrypoint, inventory: Path, home: Path
    ) -> _Collected | QualifyResult:
        implementation = entrypoint.implementation
        output_root = self.run_directory / implementation
        output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.collect_started = True
        command = [
            *entrypoint.collect,
            *collect_arguments(
                self.profile, inventory=inventory, output_root=output_root
            ),
        ]
        code, stdout, stderr = _run(
            command, environment=self._environment(home), timeout=self.collect_timeout
        )
        self._record_evidence(output_root, "collect", command, code, stdout, stderr)
        if code != 0:
            return self._failed(
                f"collect-{implementation}",
                f"{implementation} full collect exited {code}: "
                f"{bounded_diagnostic(stderr) or 'no diagnostic'}",
                FAILURE_COLLECT,
                f"Review {output_root} — the {implementation} full collect must "
                "complete in one invocation before the gate can compare anything",
            )
        archives = sorted(output_root.glob(BUNDLE_GLOB))
        if len(archives) != 1:
            return self._failed(
                f"collect-{implementation}",
                f"{implementation} full collect produced {len(archives)} bundle(s)",
                FAILURE_COLLECT,
                f"Review {output_root}: exactly one bundle per invocation is required",
            )
        archive = archives[0]

        verify_command = [*entrypoint.verify, str(archive)]
        verify_code, verify_out, verify_err = _run(
            verify_command, environment=self._environment(home), timeout=VERIFY_TIMEOUT_SECONDS
        )
        self._record_evidence(
            output_root, "verify", verify_command, verify_code, verify_out, verify_err
        )
        verify_result = "pass" if verify_code == 0 else f"fail (exit {verify_code})"
        record = RunRecord(
            implementation,
            exit_code=code,
            invocation_id=archive.name,
            bundle_path=str(archive),
            bundle_hash=_sha256(archive),
            verify_result=verify_result,
        )
        # Verification comes before anything reads the bundle's content: a bundle
        # that failed structural and content-safety checks is not evidence, so
        # the gate stops rather than describing it.
        if verify_code != 0:
            self.runs.append(record)
            return self._failed(
                f"verify-{implementation}",
                f"{implementation} bundle failed verification: "
                f"{bounded_diagnostic(verify_err) or f'exit {verify_code}'}",
                FAILURE_VERIFY,
                f"Investigate {archive} — a bundle that fails structural and "
                "content-safety verification is not qualification evidence",
            )

        try:
            contents = read_bundle(archive)
        except BundleUnreadable as error:
            self.runs.append(record)
            return self._failed(
                f"collect-{implementation}",
                str(error),
                FAILURE_BUNDLE,
                f"Review {archive}: the gate refused to read it ({error})",
            )
        record = replace(
            record,
            coverage=coverage_of(contents, [host.name for host in self.profile.hosts]),
        )
        coverage = record.coverage
        self._passed(
            f"collect-{implementation}",
            f"one invocation produced {archive.name}, verified, "
            f"{'complete' if coverage.complete else 'incomplete'} coverage",
        )
        self.node_invocation_ids += _node_invocation_ids(contents)
        return _Collected(record, contents)

    def _environment(self, home: Path) -> dict[str, str]:
        """The scrubbed environment used by the post-cutover Python invocation.

        Every `CEPH_INCIDENT_*` variable is dropped from what this process
        inherited.  The collectors read some of them as *defaults*: the shell
        reference initialises its `--allow-cephadm-shell` flag from
        `CEPH_INCIDENT_ALLOW_CEPHADM_SHELL`, so an operator who happens to have
        that exported would get a qualification run that may start a container
        while the gate still reported the opt-ins disabled.  The `..._TEST_...`
        caps are the same hazard aimed at the safety limits.  Checking the argv
        is therefore not enough; the environment is a second way in, and the gate
        closes it rather than trusting the shell it was launched from.

        `HOME` is the collector-owned directory whose `.ssh/known_hosts` holds
        only the keys the active profile trusts, so `--no-trust-ssh-host-key`
        means "pin to the reviewed identity" rather than "hope the operator's
        known_hosts is right".  `KUBECONFIG` names the profile's kubeconfig so
        the Rook layer reads the same cluster the identity preflight proved.
        """

        return {
            **{
                name: value
                for name, value in os.environ.items()
                if not name.startswith(COLLECTOR_VARIABLE_PREFIX)
            },
            "HOME": str(home),
            "KUBECONFIG": str(self.profile.rook_kubeconfig_path),
            "LC_ALL": "C",
            "TZ": "UTC",
        }

    def _record_evidence(
        self,
        output_root: Path,
        stage: str,
        command: Sequence[str],
        code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        """Keep each invocation's command, status and streams beside its bundle.

        These stay local-only next to the run rather than going into the report:
        a report is scanned for credential material before it is written, and a
        collector's stderr is exactly the place an unexpected diagnostic could
        carry some.
        """

        write_owner_only(
            output_root / f"{stage}.log",
            "\n".join(
                [
                    f"# command: {' '.join(command)}",
                    f"# exit: {code}",
                    "# stdout:",
                    stdout,
                    "# stderr:",
                    stderr,
                ]
            ),
        )

    # -- gates over the two runs -------------------------------------------

    def _check_coverage(self, run: RunRecord) -> QualifyResult | None:
        gaps = run.coverage.gaps()
        if gaps:
            return self._failed(
                f"collector-coverage-{run.implementation}",
                f"{run.implementation}: {', '.join(gaps)}",
                FAILURE_COVERAGE,
                f"Investigate the uncovered collector path(s) in {self.run_directory}: "
                "qualification requires one invocation to cover Ceph, Rook, "
                "Prometheus, every inventory node and /var/log",
            )
        self._passed(
            f"collector-coverage-{run.implementation}",
            f"the {run.implementation} invocation covered Ceph, Rook, Prometheus, "
            "every inventory node and /var/log",
        )
        return None

    def _check_workstation_cleanup(self, run: RunRecord) -> QualifyResult | None:
        output_root = self.run_directory / run.implementation
        expected = {
            Path(run.bundle_path or "").name,
            "collect.log",
            "verify.log",
        }
        leftovers = sorted(item.name for item in output_root.iterdir() if item.name not in expected)
        if leftovers:
            return self._failed(
                f"workstation-cleanup-{run.implementation}",
                f"collector-owned output still contains: {', '.join(leftovers)}",
                FAILURE_WORKSTATION_RESIDUE,
                f"Review {output_root}; a successful collect must remove its owned "
                "workdir before post-cutover proof can pass",
            )
        self._passed(
            f"workstation-cleanup-{run.implementation}",
            "successful collect left only the bundle and owner-only command ledgers",
        )
        return None

    def _compare(self, collected: Sequence[_Collected]) -> QualifyResult | None:
        reference, candidate = collected[0], collected[1]
        differences = describe_differences(
            contract_of(reference.contents, self._own_path_rules(reference.record)),
            contract_of(candidate.contents, self._own_path_rules(candidate.record)),
        )
        self.comparison = ComparisonRecord(
            result="equivalent" if not differences else "different",
            differences=differences,
        )
        if differences:
            return self._failed(
                "bundle-comparison",
                f"{len(differences)} observable-contract difference(s) between the "
                "preserved shell baseline and post-cutover Python bundle",
                FAILURE_COMPARISON,
                f"Investigate the recorded contract differences in {self.run_directory}; "
                "the preserved baseline must not be replaced to make them disappear",
            )
        self._passed(
            "bundle-comparison",
            "the preserved shell baseline and post-cutover Python bundle have "
            "equivalent normalized observable contracts",
        )
        return None

    def _own_path_rules(self, record: RunRecord) -> list[tuple[re.Pattern[str], str]]:
        """Erase each run's *own* output location before the two are compared.

        The two collects are given different `--out` directories by definition —
        that is how they avoid overwriting each other — so a shared placeholder
        would still leave `shell` against `python` in every recorded path.  Each
        side is normalised against its own directory instead.
        """

        return [
            (_path_pattern(record.bundle_path), "<bundle>"),
            (_path_pattern(str(Path(record.bundle_path).parent) if record.bundle_path else None), "<out>"),
            (_path_pattern(str(self.run_directory)), "<run>"),
        ]

    def _snapshot_unreadable(self, stage: str, error: Exception) -> QualifyResult:
        return self._failed(
            stage,
            str(error),
            FAILURE_STABLE_STATE_UNREADABLE,
            f"Restore direct read-only Ceph CLI and local kubectl access ({error}) "
            "and re-run "
            f"{qualify_command(self.profile_path, self.baseline_report)}",
        )

    def _finish(
        self,
        failure: QualifyResult,
        prober: LabProber,
        known_hosts: Path,
        baseline: dict[str, ResidueListing],
    ) -> QualifyResult:
        """Check for residue even when an earlier stage already failed.

        Once a collect has run, the lab has been touched, and "the gate stopped
        early" is not a reason to leave a node's leftovers unreported — those are
        exactly the runs most likely to have leaked, since something went wrong.
        A residue finding also *replaces* the earlier failure class: a lab left
        dirty is the finding that has to reach a person first, and the stage that
        failed before it is still in the report's checks.
        """

        if not self.collect_started:
            return failure
        residue = self._check_residue(prober, known_hosts, baseline)
        if residue is not None:
            return residue
        # `failure` was built before the residue check ran, so it still carries
        # the empty residue list; the verdict is unchanged but the evidence is
        # not, and the report gets the newer one.
        return self._result(
            failure.status, failure.next_action, blocked_reason=failure.blocked_reason
        )

    def _check_residue(
        self,
        prober: LabProber,
        known_hosts: Path,
        baseline: dict[str, ResidueListing],
    ) -> QualifyResult | None:
        """Record every node's residue verdict; return a failure only if unclean."""

        unclean: list[str] = []
        for host in self.profile.hosts:
            try:
                after = observe_residue(prober, host, known_hosts)
            except ResidueUnavailable as error:
                self.residue.append(
                    ResidueRecord(host.name, RESULT_UNREADABLE, str(error))
                )
                unclean.append(f"{host.name}: listing unreadable")
                continue
            result, detail = compare_residue(
                baseline[host.name], after, invocation_ids=self.node_invocation_ids
            )
            self.residue.append(ResidueRecord(host.name, result, detail))
            if result != RESULT_CLEAN:
                unclean.append(f"{host.name}: {detail}")
        if unclean:
            return self._failed(
                "remote-residue",
                "; ".join(unclean),
                FAILURE_RESIDUE,
                f"Review the per-node residue findings in {self.run_directory} by hand "
                "— the gate does not delete anything it cannot prove it owns",
            )
        self._passed(
            "remote-residue",
            f"no collector workspace or helper process left on {len(self.residue)} node(s)",
        )
        return None


def _node_invocation_ids(contents: BundleContents) -> tuple[str, ...]:
    """Read the node invocation identifiers a bundle recorded, if it records any.

    Only the candidate writes them (`node_invocation_id_<alias>`), so this is
    attribution help for the residue report, never a requirement: ownership is
    established by the pre/post listing comparison.
    """

    found: list[str] = []
    for line in contents.text("environment.txt").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.startswith("node_invocation_id_") and value.strip():
            found.append(value.strip())
    return tuple(found)


def _path_pattern(path: str | None) -> re.Pattern[str]:
    """Match one run's own directory, so a path that only differs by which
    implementation produced it is not read as a contract difference."""

    return re.compile(re.escape(path) if path else r"(?!)")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _run(
    command: Sequence[str], *, environment: dict[str, str], timeout: int
) -> tuple[int, str, str]:
    """Run one bounded entrypoint invocation, normalising transport failures."""

    try:
        completed = subprocess.run(
            list(command),
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return EXIT_COMMAND_MISSING, "", f"{command[0]}: command not found"
    except subprocess.TimeoutExpired:
        return EXIT_TIMEOUT, "", f"timed out after {timeout}s"
    except OSError as error:
        return EXIT_COMMAND_MISSING, "", str(error)
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )
