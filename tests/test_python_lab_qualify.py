"""The post-cutover qualification harness, driven end to end against the fake lab.

Every test here runs the real orchestration — pinned baseline, preflight, shared
inventory, snapshot, one live collect, verification, coverage, comparison and
residue — and changes exactly one thing to prove its gate is load-bearing.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import validation.lab_qualify as lab_qualify
from tests.lab_fixture import FakeLab, fake_entrypoints
from tests.test_python_lab_baseline import authority_for, write_baseline
from validation.lab_artifacts import purge_artifacts, scan_artifacts
from validation.lab_report import CodeIdentity, report_from_qualification, write_report
from validation.lab_qualify import (
    FORBIDDEN_ARGUMENTS,
    QUALIFICATION_ARGUMENTS,
    STATUS_PASS,
    default_entrypoints,
    qualify,
)


class QualifyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lab = FakeLab(self.root)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.ssh_log = self.root / "ssh.log"
        self.kubectl_log = self.root / "kubectl.log"

    def run_gate(
        self,
        profile: Path | None = None,
        checkout: Path | None = None,
        run_id: str = "run",
        **knobs: str,
    ):
        profile = profile or self.lab.write_profile()
        baseline_report, baseline_bundle = write_baseline(
            self.root, self.lab, profile
        )
        authority = authority_for(baseline_report, baseline_bundle)
        run_directory = self.runs / run_id
        run_directory.mkdir(mode=0o700, exist_ok=True)
        environment = self.lab.environment(
            FAKE_LAB_SSH_LOG=str(self.ssh_log),
            FAKE_LAB_KUBECTL_LOG=str(self.kubectl_log),
            **knobs,
        )
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch(
                "validation.lab_baseline.ISSUE_21_BASELINE", authority
            ),
        ):
            return qualify(
                profile,
                baseline_report=baseline_report,
                run_directory=run_directory,
                entrypoints=fake_entrypoints(("python",)),
                collect_timeout=120,
                repository_root=checkout or self.lab.checkout(),
            )

    def checks(self, result) -> dict[str, bool]:
        return {check.name: check.ok for check in result.checks}


class PassingGateTests(QualifyTestCase):
    def test_a_clean_lab_and_equivalent_baseline_and_live_bundle_pass(self) -> None:
        result = self.run_gate()
        self.assertEqual(result.status, STATUS_PASS, result.blocked_reason)
        self.assertTrue(result.ok)

    def test_every_stage_runs_in_the_documented_order(self) -> None:
        result = self.run_gate()
        self.assertEqual(
            [check.name for check in result.checks],
            [
                "code-identity",
                "read-only-opt-ins",
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
                "stable-state-pre",
                "residue-baseline",
                "code-identity-pre-collect",
                "collect-python",
                "collector-coverage-python",
                "workstation-cleanup-python",
                "bundle-comparison",
                "stable-state-post",
                "remote-residue",
                "code-identity-final",
            ],
        )
        self.assertTrue(all(check.ok for check in result.checks))

    def test_records_baseline_and_live_bundle_with_coverage_verify_and_hash(self) -> None:
        result = self.run_gate()
        self.assertEqual([run.implementation for run in result.runs], ["shell", "python"])
        for run in result.runs:
            self.assertEqual(run.exit_code, 0)
            self.assertEqual(run.verify_result, "pass")
            self.assertTrue(run.coverage.complete, run.coverage.document())
            self.assertTrue((run.bundle_hash or "").startswith("sha256:"))
            self.assertTrue(Path(run.bundle_path or "").is_file())

    def test_records_the_comparison_stable_state_and_every_node(self) -> None:
        result = self.run_gate()
        self.assertEqual(result.comparison.result, "equivalent")
        self.assertEqual(result.comparison.differences, ())
        self.assertEqual(result.stable_state.result, "unchanged")
        self.assertEqual(result.stable_state.schema_version, 1)
        self.assertEqual(
            [entry.host for entry in result.residue], ["monitor01", "mon02", "osd01"]
        )
        self.assertTrue(all(entry.result == "clean" for entry in result.residue))

    def test_a_pass_still_names_exactly_one_next_action(self) -> None:
        result = self.run_gate()
        self.assertNotIn("\n", result.next_action)
        self.assertIn("#22", result.next_action)
        self.assertIn(str(result.run_directory), result.next_action)

    def test_keeps_the_live_invocations_command_ledgers_beside_its_bundle(self) -> None:
        result = self.run_gate()
        collect_log = result.run_directory / "python" / "collect.log"
        verify_log = result.run_directory / "python" / "verify.log"
        self.assertTrue(collect_log.is_file())
        self.assertIn("# exit: 0", collect_log.read_text(encoding="utf-8"))
        self.assertIn("VERIFY PASS", verify_log.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(collect_log.stat().st_mode), 0o600)


class PostCutoverGateTests(QualifyTestCase):
    def run_post_cutover(self, **knobs: str):
        profile = self.lab.write_profile()
        baseline_report, baseline_bundle = write_baseline(
            self.root, self.lab, profile
        )
        authority = authority_for(baseline_report, baseline_bundle)
        run_directory = self.runs / "post-cutover"
        run_directory.mkdir(mode=0o700)
        environment = self.lab.environment(
            FAKE_LAB_SSH_LOG=str(self.ssh_log),
            FAKE_LAB_KUBECTL_LOG=str(self.kubectl_log),
            **knobs,
        )
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch(
                "validation.lab_baseline.ISSUE_21_BASELINE", authority
            ),
        ):
            result = qualify(
                profile,
                baseline_report=baseline_report,
                run_directory=run_directory,
                entrypoints=fake_entrypoints(("python",)),
                collect_timeout=120,
                repository_root=self.lab.checkout(),
            )
        return result, baseline_bundle

    def test_one_python_collect_is_compared_with_the_preserved_shell_baseline(self) -> None:
        result, baseline_bundle = self.run_post_cutover()
        run_directory = result.run_directory

        self.assertEqual(result.status, STATUS_PASS, result.blocked_reason)
        self.assertEqual(
            [run.implementation for run in result.runs], ["shell", "python"]
        )
        self.assertEqual(Path(result.runs[0].bundle_path or ""), baseline_bundle)
        self.assertTrue((run_directory / "python" / "collect.log").is_file())
        self.assertFalse((run_directory / "shell").exists())
        self.assertEqual(result.comparison.result, "equivalent")
        checks = [check.name for check in result.checks]
        self.assertLess(checks.index("baseline-evidence"), checks.index("profile-state"))
        self.assertIn("workstation-cleanup-python", checks)
        document = report_from_qualification(
            result, code=CodeIdentity("2" * 40, dirty=False)
        ).document()
        self.assertEqual(document["baseline"]["status"], "pass")
        self.assertEqual(document["baseline"]["code_commit"], "1" * 40)
        self.assertEqual(
            document["baseline"]["shell_bundle_path"], str(baseline_bundle)
        )

    def test_a_successful_collect_with_a_local_owned_workdir_cannot_pass(self) -> None:
        result, _ = self.run_post_cutover(FAKE_COLLECT_LOCAL_RESIDUE_python="1")

        self.assertEqual(result.status, "workstation-residue")
        self.assertIn("tmp.python01", result.blocked_reason or "")
        self.assertEqual(result.comparison.result, "not-run")


class SharedInputTests(QualifyTestCase):
    def test_checkout_change_after_pre_snapshot_stops_before_collect(self) -> None:
        checkout = self.lab.checkout()
        real_capture = lab_qualify.capture_stable_state
        captures = 0

        def capture_then_edit(*args, **kwargs):
            nonlocal captures
            snapshot = real_capture(*args, **kwargs)
            captures += 1
            if captures == 1:
                (checkout / "collector.py").write_text(
                    "# changed after preflight\n", encoding="utf-8"
                )
            return snapshot

        with mock.patch(
            "validation.lab_qualify.capture_stable_state",
            side_effect=capture_then_edit,
        ):
            result = self.run_gate(checkout=checkout)

        self.assertEqual(result.status, "code-identity-unclear")
        self.assertFalse((result.run_directory / "python").exists())

    def test_checkout_change_after_residue_check_cannot_return_pass(self) -> None:
        checkout = self.lab.checkout()
        real_check = lab_qualify._Qualification._check_residue

        def check_then_edit(qualification, *args, **kwargs):
            result = real_check(qualification, *args, **kwargs)
            if result is None:
                (checkout / "collector.py").write_text(
                    "# changed before PASS\n", encoding="utf-8"
                )
            return result

        with mock.patch.object(
            lab_qualify._Qualification,
            "_check_residue",
            new=check_then_edit,
        ):
            result = self.run_gate(checkout=checkout)

        self.assertEqual(result.status, "code-identity-unclear")
        self.assertTrue(all(entry.result == "clean" for entry in result.residue))

    def test_report_guard_rejects_a_commit_change_after_gate_return(self) -> None:
        result = self.run_gate()

        guarded = result.enforce_report_code_identity(
            CodeIdentity("f" * 40, dirty=False)
        )

        self.assertEqual(guarded.status, "code-identity-unclear")
        self.assertFalse(guarded.checks[-1].ok)
        self.assertEqual(guarded.checks[-1].name, "code-identity-report")

    def test_the_api_requires_a_baseline_before_any_lab_probe(self) -> None:
        profile = self.lab.write_profile()
        run_directory = self.runs / "missing-baseline"
        run_directory.mkdir(mode=0o700)

        with (
            mock.patch(
                "validation.lab_qualify.preflight",
                side_effect=AssertionError("must not touch the lab"),
            ),
            self.assertRaisesRegex(TypeError, "baseline_report"),
        ):
            qualify(
                profile,
                run_directory=run_directory,
                entrypoints=fake_entrypoints(),
                repository_root=self.lab.checkout(),
            )

    def test_baseline_and_live_bundle_record_the_same_derived_inventory(self) -> None:
        # The fixture rejects anything outside the qualification vector, so a run
        # reaching the comparison already proves both got the same shape; this
        # pins the inventory content the two bundles were built from.
        result = self.run_gate()
        seeds = {
            _environment_field(Path(run.bundle_path or ""), "seed") for run in result.runs
        }
        self.assertEqual(seeds, {"operator@10.0.0.11"})

    def test_the_forbidden_opt_ins_are_absent_from_the_collect_vector(self) -> None:
        for argument in FORBIDDEN_ARGUMENTS:
            self.assertNotIn(argument, QUALIFICATION_ARGUMENTS)
        self.assertIn("--no-trust-ssh-host-key", QUALIFICATION_ARGUMENTS)

    def test_an_exported_opt_in_cannot_reach_the_collectors(self) -> None:
        # Python uses other `CEPH_INCIDENT_*` variables for test-only safety
        # limits and source overrides. The fake collect refuses the run outright
        # if any variable in that namespace survives the scrubbed environment.
        result = self.run_gate(
            CEPH_INCIDENT_ALLOW_CEPHADM_SHELL="1",
            CEPH_INCIDENT_ALLOW_KUBECTL_EXEC="1",
            CEPH_INCIDENT_TEST_BUNDLE_SAFETY_CAP_BYTES="1",
        )
        self.assertEqual(result.status, STATUS_PASS, result.blocked_reason)
        log = (result.run_directory / "python" / "collect.log").read_text(
            encoding="utf-8"
        )
        self.assertIn("# exit: 0", log)

    def test_a_modified_checkout_never_reaches_a_collect(self) -> None:
        # A report says "these two implementations agreed" and names a commit; a
        # modified tracked file means that commit does not describe what ran.
        result = self.run_gate(checkout=self.lab.checkout(clean=False))
        self.assertEqual(result.status, "code-identity-unclear")
        self.assertIn("collector.py", result.blocked_reason or "")
        self.assertFalse((result.run_directory / "shell").exists())

    def test_the_production_entrypoint_is_the_python_implementation(self) -> None:
        (python,) = default_entrypoints()
        self.assertEqual(python.implementation, "python")
        self.assertEqual(python.collect[-1], "collect")
        self.assertEqual(python.verify[-1], "verify")


class IdentityGateTests(QualifyTestCase):
    def test_a_candidate_profile_never_reaches_a_collect(self) -> None:
        result = self.run_gate(self.lab.write_profile("candidate.toml", state="candidate"))
        self.assertEqual(result.status, "profile-not-active")
        self.assertFalse((result.run_directory / "shell").exists())
        self.assertIn("lab-profile-activate", result.next_action)

    def test_a_rotated_host_key_stops_the_run_before_any_collect(self) -> None:
        keys = json.dumps({"10.0.0.11": [["ssh-ed25519", "rebuilt-lab-key"]]})
        result = self.run_gate(FAKE_LAB_HOST_KEYS=keys)
        self.assertEqual(result.status, "ssh-fingerprint-mismatch")
        self.assertFalse((result.run_directory / "shell").exists())

    def test_a_different_ceph_cluster_stops_the_run_before_any_collect(self) -> None:
        result = self.run_gate(FAKE_LAB_CEPH_FSID="3f2b1c8e-0000-4a1d-8b7e-00000000ffff")
        self.assertEqual(result.status, "ceph-identity-mismatch")
        self.assertFalse((result.run_directory / "shell").exists())

    def test_an_unreachable_prometheus_stops_the_run_before_any_collect(self) -> None:
        result = self.run_gate(FAKE_LAB_PROM_MODE="down")
        self.assertEqual(result.status, "prometheus-not-ready")
        self.assertFalse((result.run_directory / "shell").exists())


class CollectGateTests(QualifyTestCase):
    def test_a_live_collect_that_produces_no_bundle_fails(self) -> None:
        result = self.run_gate(FAKE_COLLECT_NO_BUNDLE_python="1")
        self.assertEqual(result.status, "collect-failed")
        self.assertIn("0 bundle(s)", result.blocked_reason or "")

    def test_a_stopped_gate_still_reports_both_implementations(self) -> None:
        result = self.run_gate(FAKE_COLLECT_EXIT_python="2")
        self.assertEqual([run.implementation for run in result.runs], ["shell", "python"])
        self.assertIsNone(result.runs[1].exit_code)
        self.assertEqual(result.runs[1].verify_result, "not-run")

    def test_a_bundle_that_fails_verification_is_not_evidence(self) -> None:
        result = self.run_gate(FAKE_VERIFY_FAIL_python="1")
        self.assertEqual(result.status, "verify-failed")
        self.assertEqual(result.runs[-1].verify_result, "fail (exit 1)")
        self.assertEqual(result.comparison.result, "not-run")


class RetainedWorkdirTests(QualifyTestCase):
    """A failed run keeps its workdir, and only an operator ever takes it back.

    The retention is the read-only safety contract's, not an oversight: a gate
    that failed leaves the scene intact instead of producing a bundle that looks
    complete.  Reclaiming the disk it costs is `lab-clean`'s job — an explicit,
    confirmed step taken after the failure has been read — so these tests pin
    both halves: the gate never deletes, and the purge never takes the ledgers.
    """

    def test_a_failed_collect_keeps_everything_it_produced(self) -> None:
        result = self.run_gate(FAKE_COLLECT_EXIT_python="2")
        self.assertEqual(result.status, "collect-failed")
        output = result.run_directory / "python"
        self.assertTrue(output.is_dir())
        self.assertTrue((output / "collect.log").is_file())
        self.assertEqual(len(sorted(output.glob("ceph-incident-*.tar.gz"))), 1)

    def test_a_failed_verification_keeps_the_bundle_it_rejected(self) -> None:
        result = self.run_gate(FAKE_VERIFY_FAIL_python="1")
        self.assertEqual(result.status, "verify-failed")
        bundle = Path(result.runs[-1].bundle_path or "")
        self.assertTrue(bundle.is_file())
        self.assertTrue((result.run_directory / "python" / "verify.log").is_file())

    def test_the_gate_never_reaches_the_purge(self) -> None:
        # Belt and braces over the two tests above: if a future edit wires
        # cleanup into the gate, this fails wherever it was wired in.
        with mock.patch(
            "validation.lab_artifacts.purge_artifacts",
            side_effect=AssertionError("the gate must never purge its own workdir"),
        ):
            self.assertEqual(self.run_gate(FAKE_COLLECT_EXIT_python="2").status,
                             "collect-failed")

    def test_reclaiming_a_failed_run_keeps_its_report_and_ledgers(self) -> None:
        # Under the run id `reserve_run_directory` really gives a run: the
        # cleanup only ever looks inside directories of that exact shape.
        result = self.run_gate(run_id="20260801T230957Z", FAKE_COLLECT_EXIT_python="2")
        write_report(
            self.runs,
            report_from_qualification(result, code=CodeIdentity("0" * 40, dirty=False)),
            directory=result.run_directory,
        )
        retained = scan_artifacts(self.runs)
        self.assertEqual(retained.count, 1)
        self.assertGreater(retained.size, 0)

        preview = purge_artifacts(self.runs, confirmed=False, keep=0)
        self.assertFalse(preview.ok)
        self.assertEqual(scan_artifacts(self.runs).count, 1)

        purged = purge_artifacts(self.runs, confirmed=True, keep=0)
        self.assertTrue(purged.ok)
        self.assertEqual(sorted(item.name for item in result.run_directory.iterdir()),
                         ["python", "report.json", "report.md"])
        self.assertEqual(
            sorted(item.name for item in (result.run_directory / "python").iterdir()),
            ["collect.log"],
        )


class CoverageGateTests(QualifyTestCase):
    def test_a_missing_collector_path_fails_the_gate(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DROP_python="prometheus")
        self.assertEqual(result.status, "coverage-incomplete")
        self.assertIn("prometheus=missing", result.blocked_reason or "")
        self.assertEqual(result.comparison.result, "not-run")

    def test_a_documented_skip_is_still_incomplete_coverage(self) -> None:
        result = self.run_gate(FAKE_COLLECT_SKIP_python="rook")
        self.assertEqual(result.status, "coverage-incomplete")
        self.assertIn("rook=skipped", result.blocked_reason or "")

    def test_a_node_without_var_log_fails_the_gate(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DROP_python="varlog")
        self.assertEqual(result.status, "coverage-incomplete")
        self.assertIn("var_log=mon02=missing, monitor01=missing, osd01=missing", result.blocked_reason or "")


class ComparisonGateTests(QualifyTestCase):
    def test_live_cluster_drift_between_baseline_and_current_is_not_a_difference(self) -> None:
        # The fixture stamps a different clock and a different counter into every
        # bundle, exactly as a live cluster would between the preserved baseline
        # and the current collect.
        result = self.run_gate()
        self.assertEqual(result.comparison.result, "equivalent")

    def test_each_runs_own_output_directory_is_not_a_difference(self) -> None:
        # The preserved bundle and current collect live in different output
        # directories, and both record absolute artifact paths. Those paths must
        # normalise to the same thing.
        result = self.run_gate()
        self.assertNotEqual(result.runs[0].bundle_path, result.runs[1].bundle_path)
        self.assertEqual(result.comparison.result, "equivalent")

    def test_an_extra_live_artifact_fails_the_gate(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DIVERGE_python="extra-artifact")
        self.assertEqual(result.status, "bundle-comparison-failed")
        self.assertTrue(
            any("uptime.txt" in line for line in result.comparison.differences),
            result.comparison.differences,
        )

    def test_a_different_recorded_exit_code_fails_the_gate(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DIVERGE_python="manifest-exit")
        self.assertEqual(result.status, "bundle-comparison-failed")

    def test_evidence_that_stopped_being_json_fails_the_gate(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DIVERGE_python="not-json")
        self.assertEqual(result.status, "bundle-comparison-failed")
        self.assertTrue(
            any("status.json" in line for line in result.comparison.differences),
            result.comparison.differences,
        )

    def test_a_different_runner_selection_fails_the_gate(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DIVERGE_python="selection")
        self.assertEqual(result.status, "bundle-comparison-failed")
        self.assertTrue(
            any("ceph_runner" in line for line in result.comparison.differences),
            result.comparison.differences,
        )

    def test_a_comparison_failure_never_reaches_the_post_snapshot(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DIVERGE_python="not-json")
        self.assertNotIn("stable-state-post", self.checks(result))
        self.assertEqual(result.stable_state.result, "not-run")


class StableStateGateTests(QualifyTestCase):
    def test_an_unreadable_snapshot_stops_before_any_collect(self) -> None:
        result = self.run_gate(FAKE_LAB_CEPH_STATE_FAIL="config dump --format json")
        self.assertEqual(result.status, "stable-state-unreadable")
        self.assertFalse((result.run_directory / "shell").exists())

    def test_a_changed_stable_field_stops_the_cutover(self) -> None:
        # The fake lab reports a different persistent option once the marker the
        # collects create exists, so the change lands *between* the snapshots.
        with mock.patch(
            "validation.lab_qualify.capture_stable_state", side_effect=_drifting_capture()
        ):
            result = self.run_gate()
        self.assertEqual(result.status, "stable-state-changed")
        self.assertEqual(result.stable_state.result, "changed")
        self.assertTrue(result.stable_state.differences)
        self.assertIn("persistent state", result.next_action)


class ResidueGateTests(QualifyTestCase):
    def test_a_leaked_workspace_stops_the_cutover(self) -> None:
        result = self.run_gate(FAKE_COLLECT_RESIDUE_python="1")
        self.assertEqual(result.status, "remote-residue")
        leaked = [entry for entry in result.residue if entry.result == "residue"]
        self.assertEqual(len(leaked), 3)
        self.assertIn("ceph-incident-node.python0001", leaked[0].detail)
        self.assertIn("by hand", result.next_action)

    def test_a_collect_that_failed_part_way_still_owes_the_nodes_a_check(self) -> None:
        # This run produced no usable record at all, but it reached the nodes.
        result = self.run_gate(FAKE_COLLECT_EXIT_python="2")
        self.assertEqual(result.status, "collect-failed")
        self.assertEqual(len(result.residue), 3)
        self.assertTrue(all(entry.result == "clean" for entry in result.residue))

    def test_residue_is_still_checked_after_an_earlier_stage_failed(self) -> None:
        # The runs most likely to have leaked are the ones where something went
        # wrong, so "the gate stopped early" must not mean the nodes go unchecked.
        result = self.run_gate(FAKE_COLLECT_DIVERGE_python="not-json")
        self.assertEqual(result.status, "bundle-comparison-failed")
        self.assertEqual(len(result.residue), 3)
        self.assertTrue(all(entry.result == "clean" for entry in result.residue))
        self.assertTrue(self.checks(result)["remote-residue"])

    def test_residue_found_after_an_earlier_failure_becomes_the_verdict(self) -> None:
        # A lab left dirty is the finding that has to reach a person first; the
        # stage that failed before it is still recorded in the checks.
        result = self.run_gate(
            FAKE_COLLECT_DIVERGE_python="not-json", FAKE_COLLECT_RESIDUE_python="1"
        )
        self.assertEqual(result.status, "remote-residue")
        self.assertFalse(self.checks(result)["bundle-comparison"])
        self.assertIn("by hand", result.next_action)

    def test_a_leftover_that_predates_the_run_is_not_blamed_on_it(self) -> None:
        self.lab.leave_residue("10.0.0.21", "workspace\t/tmp/ceph-incident-node.older")
        result = self.run_gate()
        self.assertEqual(result.status, STATUS_PASS, result.blocked_reason)
        osd = [entry for entry in result.residue if entry.host == "osd01"][0]
        self.assertEqual(osd.result, "clean")
        self.assertIn("pre-existing", osd.detail)

    def test_a_node_that_stops_answering_fails_closed(self) -> None:
        result = self.run_gate(FAKE_LAB_RESIDUE_FAIL="10.0.0.12")
        self.assertEqual(result.status, "residue-baseline-unreadable")
        self.assertFalse((result.run_directory / "shell").exists())


def _environment_field(bundle: Path, key: str) -> str:
    import tarfile

    with tarfile.open(bundle, "r:gz") as archive:
        stream = archive.extractfile("./environment.txt")
        assert stream is not None
        for line in stream.read().decode("utf-8").splitlines():
            name, marker, value = line.partition("=")
            if marker and name == key:
                return value
    return ""


def _drifting_capture():
    """Return a `capture_stable_state` that reports a change on its second call."""

    from validation.lab_snapshot import StableStateSnapshot

    calls = {"count": 0}

    def capture(prober, profile, known_hosts):
        calls["count"] += 1
        return StableStateSnapshot(
            1, {"ceph_config": [{"section": "global", "name": "debug_ms", "value": str(calls["count"])}]}
        )

    return capture


if __name__ == "__main__":
    unittest.main()
