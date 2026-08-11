from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import OTHER_FSID, FakeLab, fake_runtime_identity
from validation.lab_activation import activate
from validation.lab_discovery import discover
from validation.lab_preflight import preflight
from validation.lab_report import CodeIdentity, report_from_preflight, write_report
from validation.lab_status import lab_status


CODE = CodeIdentity(commit="0" * 40, dirty=False)


class StatusTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lab = FakeLab(Path(self.directory.name))
        self.runs = Path(self.directory.name) / "lab-validation"

    def status(self, path: Path):
        # Status must be a pure local read: give it a PATH with no commands at all,
        # so any subprocess it tried to run would fail loudly.
        with mock.patch.dict(os.environ, {"PATH": str(self.directory.name)}):
            return lab_status(path, runs_directory=self.runs)

    def record_preflight(self, profile: Path, **knobs: str) -> None:
        with mock.patch.dict(os.environ, self.lab.environment(**knobs)):
            result = preflight(profile)
        write_report(self.runs, report_from_preflight(result, code=CODE))

    def make_candidate(self, bootstrap: Path, **knobs: str) -> Path:
        with mock.patch.dict(os.environ, self.lab.environment(**knobs)):
            result = discover(bootstrap, replace_candidate=True)
        return result.candidate_path


class MissingOrInvalidProfileTests(StatusTestCase):
    def test_reports_a_missing_profile_and_points_at_the_example(self) -> None:
        status = self.status(self.lab.profiles / "absent.toml")
        self.assertEqual(status.state, "profile-missing")
        self.assertFalse(status.ready)
        self.assertIn("lab-bootstrap.example.toml", status.next_action)
        self.assertIn("lab-profile-discover", status.next_action)

    def test_reports_an_unparseable_profile(self) -> None:
        broken = self.lab.profiles / "broken.toml"
        broken.write_text("not a lab profile\n", encoding="utf-8")
        status = self.status(broken)
        self.assertEqual(status.state, "profile-invalid")
        self.assertIn("cannot parse", status.profile_error or "")
        self.assertIn("lab-status", status.next_action)

    def test_reports_a_profile_that_is_missing_identity(self) -> None:
        incomplete = self.lab.profiles / "incomplete.toml"
        incomplete.write_text(
            self.lab.profile_text(state="active", identity=False), encoding="utf-8"
        )
        status = self.status(incomplete)
        self.assertEqual(status.state, "profile-invalid")
        self.assertIn("missing lab identity", status.profile_error or "")


class ProfileStateTests(StatusTestCase):
    def test_a_bootstrap_profile_points_at_discovery(self) -> None:
        status = self.status(self.lab.write_profile(state="bootstrap", identity=False))
        self.assertEqual(status.state, "profile-bootstrap")
        self.assertIn("lab-profile-discover", status.next_action)

    def test_a_candidate_profile_points_at_activation(self) -> None:
        status = self.status(self.lab.write_profile(state="candidate"))
        self.assertEqual(status.state, "profile-candidate")
        self.assertIn("lab-profile-activate", status.next_action)
        self.assertIn("CEPH_INCIDENT_LAB_ACTIVATE=1", status.next_action)

    def test_an_active_profile_with_no_history_points_at_the_preflight(self) -> None:
        status = self.status(self.lab.write_profile())
        self.assertEqual(status.state, "ready-for-preflight")
        self.assertTrue(status.ready)
        self.assertIn("lab-preflight", status.next_action)
        self.assertIn("CEPH_INCIDENT_LAB_CONFIRM=1", status.next_action)


class CredentialPathTests(StatusTestCase):
    def test_reports_a_missing_credential_path_without_reading_it(self) -> None:
        profile = self.lab.write_profile(ssh_key=self.lab.credentials / "absent")
        status = self.status(profile)
        self.assertEqual(status.state, "credential-path-invalid")
        self.assertIn("does not exist", status.blocked_reason or "")
        self.assertEqual(
            [entry.usable for entry in status.credential_paths], [False, True]
        )

    def test_reports_a_world_readable_credential_path(self) -> None:
        self.lab.kubeconfig.chmod(0o644)
        status = self.status(self.lab.write_profile())
        self.assertEqual(status.state, "credential-path-invalid")
        self.assertIn("readable by other users", status.blocked_reason or "")

    def test_never_reveals_credential_content(self) -> None:
        status = self.status(self.lab.write_profile())
        serialised = json.dumps(status.summary()) + status.text()
        self.assertNotIn("placeholder", serialised)
        self.assertIn("id_ed25519", serialised)


class PendingCandidateTests(StatusTestCase):
    def test_a_drifted_candidate_beside_an_active_profile_blocks(self) -> None:
        bootstrap = self.lab.write_profile("lab.toml", state="bootstrap", identity=False)
        candidate = self.make_candidate(bootstrap)
        activate(candidate, bootstrap, confirmed=True)
        self.make_candidate(bootstrap, FAKE_LAB_CEPH_FSID=OTHER_FSID)
        status = self.status(bootstrap)
        self.assertEqual(status.state, "candidate-pending-review")
        self.assertIn("awaiting review", status.blocked_reason or "")
        self.assertIn("lab-profile-activate", status.next_action)
        self.assertIn("--replace-active", status.next_action)

    def test_a_leftover_candidate_that_matches_the_active_profile_does_not_block(self) -> None:
        bootstrap = self.lab.write_profile("lab.toml", state="bootstrap", identity=False)
        candidate = self.make_candidate(bootstrap)
        activate(candidate, bootstrap, confirmed=True)
        status = self.status(bootstrap)
        self.assertEqual(status.state, "ready-for-preflight")
        self.assertEqual(len(status.candidates), 1)
        self.assertTrue(status.candidates[0].matches_active)
        self.assertFalse(status.candidates[0].pending_review)

    def test_a_bootstrap_profile_with_a_candidate_asks_for_review_not_rediscovery(self) -> None:
        bootstrap = self.lab.write_profile("lab.toml", state="bootstrap", identity=False)
        candidate = self.make_candidate(bootstrap)
        status = self.status(bootstrap)
        self.assertEqual(status.state, "candidate-pending-review")
        self.assertIn(str(candidate), status.next_action)
        self.assertNotIn("lab-profile-discover", status.next_action)
        self.assertNotIn("--replace-active", status.next_action)

    def test_a_broken_credential_path_outranks_a_pending_candidate(self) -> None:
        bootstrap = self.lab.write_profile("lab.toml", state="bootstrap", identity=False)
        self.make_candidate(bootstrap)
        self.lab.ssh_key.unlink()
        status = self.status(bootstrap)
        self.assertEqual(status.state, "credential-path-invalid")

    def test_an_unreadable_candidate_is_reported_but_does_not_hide_the_profile(self) -> None:
        active = self.lab.write_profile("lab.toml")
        (self.lab.profiles / "junk.candidate.toml").write_text("junk\n", encoding="utf-8")
        status = self.status(active)
        self.assertEqual(len(status.candidates), 1)
        self.assertIsNotNone(status.candidates[0].problem)
        self.assertEqual(status.state, "ready-for-preflight")


class ActivationHistoryTests(StatusTestCase):
    def test_reports_the_last_activation_record(self) -> None:
        bootstrap = self.lab.write_profile("lab.toml", state="bootstrap", identity=False)
        candidate = self.make_candidate(bootstrap)
        result = activate(candidate, bootstrap, confirmed=True)
        status = self.status(bootstrap)
        self.assertIsNotNone(status.last_activation)
        self.assertEqual(
            status.last_activation["active_profile_hash"], result.active_hash
        )
        self.assertIn("last activation", status.text())


class ReportHistoryTests(StatusTestCase):
    def test_a_passing_preflight_report_hands_off_to_the_full_gate(self) -> None:
        profile = self.lab.write_profile()
        self.record_preflight(profile)
        status = self.status(profile)
        self.assertEqual(status.state, "preflight-passed")
        self.assertTrue(status.ready)
        self.assertIn("make validate-lab", status.next_action)
        self.assertIn(str(profile), status.next_action)

    def test_a_failed_attempt_repeats_the_reports_next_action(self) -> None:
        profile = self.lab.write_profile()
        self.record_preflight(profile, FAKE_LAB_PROM_MODE="down")
        status = self.status(profile)
        self.assertEqual(status.state, "last-attempt-failed")
        self.assertIn("prometheus-not-ready", status.blocked_reason or "")
        self.assertIn("Prometheus", status.next_action)

    def test_a_report_for_a_different_profile_hash_is_stale(self) -> None:
        profile = self.lab.write_profile()
        self.record_preflight(profile)
        profile.write_text(
            self.lab.profile_text(ceph_fsid=OTHER_FSID), encoding="utf-8"
        )
        status = self.status(profile)
        self.assertEqual(status.state, "ready-for-preflight")
        self.assertIn("lab-preflight", status.next_action)

    def test_a_report_without_a_usable_next_action_falls_back_to_a_local_one(self) -> None:
        profile = self.lab.write_profile()
        self.record_preflight(profile, FAKE_LAB_PROM_MODE="down")
        latest = (self.runs / "LATEST").read_text(encoding="utf-8").strip()
        report_path = self.runs / latest / "report.json"
        document = json.loads(report_path.read_text(encoding="utf-8"))
        for damaged in (None, "", "first\nsecond"):
            with self.subTest(next_action=damaged):
                document["next_action"] = damaged
                report_path.write_text(json.dumps(document), encoding="utf-8")
                status = self.status(profile)
                self.assertEqual(status.state, "last-attempt-failed")
                self.assertNotIn("None", status.next_action)
                self.assertIn("fix the recorded failure", status.next_action)
                self.assertNotIn("\n", status.next_action)

    def test_a_full_gate_pass_is_reported_as_its_own_state(self) -> None:
        profile = self.lab.write_profile()
        self.record_preflight(profile)
        latest = (self.runs / "LATEST").read_text(encoding="utf-8").strip()
        report_path = self.runs / latest / "report.json"
        document = json.loads(report_path.read_text(encoding="utf-8"))
        # Only issue #20's dual-run gate can record this, but a stored `pass` must
        # not be flattened into "preflight passed".
        document["status"] = "pass"
        document["next_action"] = "Proceed to the cutover ticket"
        witness = fake_runtime_identity().document()
        probe = {
            "host": "monitor01",
            "exit_code": 0,
            "status": "ok",
            "runtime": witness,
            "detail": "runtime probe on monitor01: exit 0",
        }
        document["runtime"] = {
            "tooling": fake_runtime_identity(minor=11).document(),
            "production": fake_runtime_identity().document(),
            "nodes": {"pre": {"probes": [probe]}, "post": {"probes": [probe]}},
            "comparison": {"result": "unchanged", "differences": []},
            "floor_witness": "monitor01",
        }
        document["baseline"]["status"] = "pass"
        document["runs"] = [
            {
                "implementation": "python",
                "exit_code": 0,
                "invocation_id": "run",
                "bundle_path": "/tmp/bundle.tar.gz",
                "bundle_hash": "sha256:aa",
                "verify_result": "pass",
                "coverage": {
                    name: "collected"
                    for name in ("ceph", "rook", "prometheus", "nodes", "var_log")
                },
            }
        ]
        document["comparison"] = {"result": "equivalent", "differences": []}
        document["stable_state"] = {
            "snapshot_schema_version": 1,
            "result": "unchanged",
            "differences": [],
        }
        document["residue"] = [
            {"host": "monitor01", "result": "clean", "detail": "no residue"}
        ]
        report_path.write_text(json.dumps(document), encoding="utf-8")
        status = self.status(profile)
        self.assertEqual(status.state, "gate-passed")
        self.assertTrue(status.ready)
        self.assertEqual(status.next_action, "Proceed to the cutover ticket")

    def test_a_schema_v2_pass_remains_historical_not_python310_proof(self) -> None:
        profile = self.lab.write_profile()
        self.record_preflight(profile)
        latest = (self.runs / "LATEST").read_text(encoding="utf-8").strip()
        report_path = self.runs / latest / "report.json"
        document = json.loads(report_path.read_text(encoding="utf-8"))
        document["schema_version"] = 2
        document["status"] = "pass"
        document["next_action"] = "Old post-cutover handoff"
        report_path.write_text(json.dumps(document), encoding="utf-8")

        status = self.status(profile)

        self.assertEqual(status.state, "historical-gate-passed")
        self.assertIn("schema-v2", status.blocked_reason or "")
        self.assertIn("make validate-lab", status.next_action)

    def test_reports_that_there_is_no_report_yet(self) -> None:
        status = self.status(self.lab.write_profile())
        self.assertIsNone(status.latest_report)
        self.assertIn("latest report:  none", status.text())


class RetainedArtifactTests(StatusTestCase):
    """What earlier runs left on disk, reported but never turned into a step."""

    def leave_run_artifacts(self, run_id: str, size: int = 4096) -> Path:
        run = self.runs / run_id
        (run / "shell").mkdir(parents=True)
        (run / "report.json").write_text("{}\n", encoding="utf-8")
        (run / "shell" / "collect.log").write_text("# exit: 2\n", encoding="utf-8")
        (run / "shell" / f"ceph-incident-{run_id}.tar.gz").write_bytes(b"\0" * size)
        return run

    def test_reports_the_count_total_size_and_oldest_retained_run(self) -> None:
        self.leave_run_artifacts("20260801T230957Z", size=2048)
        self.leave_run_artifacts("20260802T070709Z", size=4096)
        status = self.status(self.lab.write_profile())
        retained = status.summary()["retained_artifacts"]
        self.assertEqual(retained["runs"], 2)
        self.assertEqual(retained["bytes"], 6144)
        self.assertEqual(retained["oldest_run"], "20260801T230957Z")
        self.assertEqual(retained["oldest_timestamp"], "2026-08-01T23:09:57Z")
        self.assertIn("run artifacts:  2 run(s), 6.0 KiB", status.text())
        self.assertIn("20260801T230957Z", status.text())

    def test_says_so_plainly_when_nothing_is_retained(self) -> None:
        status = self.status(self.lab.write_profile())
        self.assertEqual(status.summary()["retained_artifacts"]["runs"], 0)
        self.assertIn("run artifacts:  none retained", status.text())

    def test_retained_artifacts_never_become_the_next_action(self) -> None:
        # A failed run keeps its workdir deliberately, so however much has piled
        # up, the workflow's own next step is still the only one offered.
        for _ in range(4):
            self.leave_run_artifacts(f"2026080{_}T230957Z", size=1024 * 1024)
        profile = self.lab.write_profile()
        self.record_preflight(profile)
        status = self.status(profile)
        self.assertEqual(status.state, "preflight-passed")
        self.assertIn("make validate-lab", status.next_action)
        self.assertNotIn("lab-clean", status.next_action)

    def test_a_status_read_never_removes_a_retained_artifact(self) -> None:
        run = self.leave_run_artifacts("20260801T230957Z")
        self.status(self.lab.write_profile())
        self.assertTrue(
            (run / "shell" / "ceph-incident-20260801T230957Z.tar.gz").is_file()
        )


class OutputTests(StatusTestCase):
    def test_the_text_output_ends_with_exactly_one_next_action(self) -> None:
        for path in (
            self.lab.write_profile("active.toml"),
            self.lab.write_profile("boot.toml", state="bootstrap", identity=False),
            self.lab.profiles / "absent.toml",
        ):
            with self.subTest(path=path.name):
                text = self.status(path).text()
                self.assertEqual(
                    [line for line in text.splitlines() if line.startswith("next action:")],
                    [f"next action: {self.status(path).next_action}"],
                )

    def test_the_summary_names_every_state_a_fresh_agent_needs(self) -> None:
        summary = self.status(self.lab.write_profile()).summary()
        for key in (
            "profile",
            "profile_state",
            "profile_hash",
            "identity_complete",
            "hosts",
            "credential_paths",
            "candidates",
            "last_activation",
            "latest_report",
            "runs_directory",
            "retained_artifacts",
            "state",
            "next_action",
        ):
            self.assertIn(key, summary)


if __name__ == "__main__":
    unittest.main()
