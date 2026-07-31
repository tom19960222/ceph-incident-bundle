"""The dual-run qualification harness, driven end to end against the fake lab.

Every test here runs the real orchestration — preflight, shared inventory,
snapshot, two collects, verification, coverage, comparison, residue — and changes
exactly one thing to prove the corresponding gate is load-bearing.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import FakeLab, fake_entrypoints
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

    def run_gate(self, profile: Path | None = None, **knobs: str):
        profile = profile or self.lab.write_profile()
        run_directory = self.runs / "run"
        run_directory.mkdir(mode=0o700, exist_ok=True)
        environment = self.lab.environment(
            FAKE_LAB_SSH_LOG=str(self.ssh_log),
            FAKE_LAB_KUBECTL_LOG=str(self.kubectl_log),
            **knobs,
        )
        with mock.patch.dict(os.environ, environment):
            return qualify(
                profile,
                run_directory=run_directory,
                entrypoints=fake_entrypoints(),
                collect_timeout=120,
            )

    def checks(self, result) -> dict[str, bool]:
        return {check.name: check.ok for check in result.checks}


class PassingGateTests(QualifyTestCase):
    def test_a_clean_lab_and_two_equivalent_collects_pass(self) -> None:
        result = self.run_gate()
        self.assertEqual(result.status, STATUS_PASS, result.blocked_reason)
        self.assertTrue(result.ok)

    def test_every_stage_runs_in_the_documented_order(self) -> None:
        result = self.run_gate()
        self.assertEqual(
            [check.name for check in result.checks],
            [
                "read-only-opt-ins",
                "profile-state",
                "credential-paths",
                "ssh-fingerprints",
                "required-hosts",
                "ceph-identity",
                "rook-identity",
                "prometheus-readiness",
                "shared-inventory",
                "stable-state-pre",
                "residue-baseline",
                "collect-shell",
                "collect-python",
                "collector-coverage",
                "bundle-comparison",
                "stable-state-post",
                "remote-residue",
            ],
        )
        self.assertTrue(all(check.ok for check in result.checks))

    def test_records_both_runs_with_coverage_verify_and_a_bundle_hash(self) -> None:
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
        self.assertIn("#21", result.next_action)
        self.assertIn(str(result.run_directory), result.next_action)

    def test_keeps_each_invocations_command_ledger_beside_its_bundle(self) -> None:
        result = self.run_gate()
        for implementation in ("shell", "python"):
            collect_log = result.run_directory / implementation / "collect.log"
            verify_log = result.run_directory / implementation / "verify.log"
            self.assertTrue(collect_log.is_file())
            self.assertIn("# exit: 0", collect_log.read_text(encoding="utf-8"))
            self.assertIn("VERIFY PASS", verify_log.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(collect_log.stat().st_mode), 0o600)


class SharedInputTests(QualifyTestCase):
    def test_both_implementations_receive_the_same_derived_inventory(self) -> None:
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

    def test_the_production_entrypoints_are_the_real_two_implementations(self) -> None:
        shell, python = default_entrypoints()
        self.assertEqual(shell.implementation, "shell")
        self.assertTrue(shell.collect[-1].endswith("run/collect.sh"))
        self.assertTrue(shell.verify[-1].endswith("lib/verify-bundle.sh"))
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
    def test_a_failing_reference_collect_stops_before_the_candidate_runs(self) -> None:
        result = self.run_gate(FAKE_COLLECT_EXIT_shell="2")
        self.assertEqual(result.status, "collect-failed")
        self.assertFalse(self.checks(result)["collect-shell"])
        self.assertNotIn("collect-python", self.checks(result))
        self.assertFalse((result.run_directory / "python").exists())

    def test_a_candidate_that_produces_no_bundle_fails(self) -> None:
        result = self.run_gate(FAKE_COLLECT_NO_BUNDLE_python="1")
        self.assertEqual(result.status, "collect-failed")
        self.assertIn("0 bundle(s)", result.blocked_reason or "")

    def test_a_stopped_gate_still_reports_both_implementations(self) -> None:
        result = self.run_gate(FAKE_COLLECT_EXIT_shell="2")
        self.assertEqual([run.implementation for run in result.runs], ["shell", "python"])
        self.assertIsNone(result.runs[1].exit_code)
        self.assertEqual(result.runs[1].verify_result, "not-run")

    def test_a_bundle_that_fails_verification_is_not_evidence(self) -> None:
        result = self.run_gate(FAKE_VERIFY_FAIL_python="1")
        self.assertEqual(result.status, "verify-failed")
        self.assertEqual(result.runs[-1].verify_result, "fail (exit 1)")
        self.assertEqual(result.comparison.result, "not-run")


class CoverageGateTests(QualifyTestCase):
    def test_a_missing_collector_path_fails_the_gate(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DROP_python="prometheus")
        self.assertEqual(result.status, "coverage-incomplete")
        self.assertIn("prometheus=missing", result.blocked_reason or "")
        self.assertEqual(result.comparison.result, "not-run")

    def test_a_documented_skip_is_still_incomplete_coverage(self) -> None:
        result = self.run_gate(FAKE_COLLECT_SKIP_shell="rook")
        self.assertEqual(result.status, "coverage-incomplete")
        self.assertIn("rook=skipped", result.blocked_reason or "")

    def test_a_node_without_var_log_fails_the_gate(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DROP_shell="varlog")
        self.assertEqual(result.status, "coverage-incomplete")
        self.assertIn("var_log=missing", result.blocked_reason or "")


class ComparisonGateTests(QualifyTestCase):
    def test_live_cluster_drift_between_the_two_runs_is_not_a_difference(self) -> None:
        # The fixture stamps a different clock and a different counter into every
        # bundle, exactly as a live cluster would between two collects.
        result = self.run_gate()
        self.assertEqual(result.comparison.result, "equivalent")

    def test_each_runs_own_output_directory_is_not_a_difference(self) -> None:
        # The two collects must write somewhere different or they would overwrite
        # each other, and both record artifacts by absolute path — so the paths
        # differ by construction and must normalise to the same thing.
        result = self.run_gate()
        self.assertNotEqual(result.runs[0].bundle_path, result.runs[1].bundle_path)
        self.assertEqual(result.comparison.result, "equivalent")

    def test_an_extra_candidate_artifact_fails_the_gate(self) -> None:
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
