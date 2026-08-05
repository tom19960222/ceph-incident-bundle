from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.lab_fixture import OTHER_FSID, FakeLab
from validation.lab_profile import load_profile


ROOT = Path(__file__).resolve().parents[1]


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lab = FakeLab(Path(self.directory.name))
        self.runs = Path(self.directory.name) / "lab-validation"

    def run_cli(self, *arguments: str, **knobs: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "validation.lab", *arguments],
            cwd=ROOT,
            env={**os.environ, **self.lab.environment(**knobs)},
            text=True,
            capture_output=True,
            check=False,
        )

    def next_action(self, completed: subprocess.CompletedProcess[str]) -> str:
        actions = [
            line
            for line in (completed.stdout + completed.stderr).splitlines()
            if line.startswith("next action:")
        ]
        self.assertEqual(len(actions), 1, completed.stdout + completed.stderr)
        return actions[0]


class UsageTests(CliTestCase):
    def test_no_arguments_prints_usage_and_exits_one(self) -> None:
        completed = self.run_cli()
        self.assertEqual(completed.returncode, 1)
        self.assertIn("Usage:", completed.stderr)

    def test_an_unknown_command_or_option_exits_one(self) -> None:
        for arguments in (
            ("frobnicate", "--profile", "/labs/lab.toml"),
            ("status", "--profile", "/labs/lab.toml", "--force"),
            ("status",),
        ):
            with self.subTest(arguments=arguments):
                completed = self.run_cli(*arguments)
                self.assertEqual(completed.returncode, 1)
                self.assertIn("Usage:", completed.stderr)

    def test_a_relative_profile_path_is_refused(self) -> None:
        completed = self.run_cli("status", "--profile", "lab.toml")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("must be an absolute path", completed.stderr)

    def test_command_specific_options_are_not_accepted_elsewhere(self) -> None:
        for arguments in (
            ("status", "--profile", "/labs/lab.toml", "--replace-candidate"),
            ("discover", "--profile", "/labs/lab.toml", "--replace-active"),
            ("status", "--profile", "/labs/lab.toml", "--candidate", "/labs/c.toml"),
        ):
            with self.subTest(arguments=arguments):
                completed = self.run_cli(*arguments)
                self.assertEqual(completed.returncode, 1)


class StatusCommandTests(CliTestCase):
    def test_reports_a_ready_profile_and_exits_zero(self) -> None:
        profile = self.lab.write_profile()
        completed = self.run_cli(
            "status", "--profile", str(profile), "--runs-dir", str(self.runs)
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ready-for-preflight", completed.stdout)
        self.assertIn("next action: Run make lab-preflight", completed.stdout)

    def test_a_blocked_profile_exits_two_with_one_next_action(self) -> None:
        profile = self.lab.write_profile(state="bootstrap", identity=False)
        completed = self.run_cli(
            "status", "--profile", str(profile), "--runs-dir", str(self.runs)
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("lab-profile-discover", self.next_action(completed))

    def test_json_output_is_machine_readable(self) -> None:
        profile = self.lab.write_profile()
        completed = self.run_cli(
            "status", "--profile", str(profile), "--runs-dir", str(self.runs), "--json"
        )
        document = json.loads(completed.stdout)
        self.assertEqual(document["state"], "ready-for-preflight")
        self.assertEqual(document["profile_state"], "active")
        self.assertIn("next_action", document)


class DiscoverCommandTests(CliTestCase):
    def test_writes_a_candidate_and_exits_zero(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        completed = self.run_cli("discover", "--profile", str(bootstrap))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        candidate = bootstrap.parent / "lab.candidate.toml"
        self.assertEqual(load_profile(candidate).state, "candidate")
        self.assertIn("host key monitor01: ok", completed.stdout)
        self.assertIn("lab-profile-activate", self.next_action(completed))

    def test_an_incomplete_discovery_exits_two_and_writes_nothing(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        completed = self.run_cli(
            "discover", "--profile", str(bootstrap), FAKE_LAB_CEPH_FAIL="1"
        )
        self.assertEqual(completed.returncode, 2)
        self.assertFalse((bootstrap.parent / "lab.candidate.toml").exists())
        self.assertIn("ceph fsid", self.next_action(completed))

    def test_honours_an_explicit_candidate_destination(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        destination = self.lab.profiles / "elsewhere.candidate.toml"
        completed = self.run_cli(
            "discover",
            "--profile",
            str(bootstrap),
            "--candidate-out",
            str(destination),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(destination.is_file())


class ActivateCommandTests(CliTestCase):
    def candidate(self) -> tuple[Path, Path]:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        self.run_cli("discover", "--profile", str(bootstrap))
        return bootstrap, bootstrap.parent / "lab.candidate.toml"

    def test_requires_the_activation_confirmation(self) -> None:
        bootstrap, candidate = self.candidate()
        completed = self.run_cli(
            "activate", "--profile", str(bootstrap), "--candidate", str(candidate)
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("CEPH_INCIDENT_LAB_ACTIVATE=1", self.next_action(completed))
        self.assertEqual(load_profile(bootstrap).state, "bootstrap")

    def test_activates_with_the_confirmation(self) -> None:
        bootstrap, candidate = self.candidate()
        completed = self.run_cli(
            "activate",
            "--profile",
            str(bootstrap),
            "--candidate",
            str(candidate),
            CEPH_INCIDENT_LAB_ACTIVATE="1",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(load_profile(bootstrap).state, "active")
        self.assertIn("lab-status", self.next_action(completed))

    def test_refuses_to_replace_an_active_profile_without_the_second_opt_in(self) -> None:
        bootstrap, candidate = self.candidate()
        self.run_cli(
            "activate",
            "--profile",
            str(bootstrap),
            "--candidate",
            str(candidate),
            CEPH_INCIDENT_LAB_ACTIVATE="1",
        )
        self.run_cli(
            "discover",
            "--profile",
            str(bootstrap),
            "--replace-candidate",
            FAKE_LAB_CEPH_FSID=OTHER_FSID,
        )
        completed = self.run_cli(
            "activate",
            "--profile",
            str(bootstrap),
            "--candidate",
            str(candidate),
            CEPH_INCIDENT_LAB_ACTIVATE="1",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--replace-active", self.next_action(completed))

    def test_requires_a_candidate_path(self) -> None:
        bootstrap, _ = self.candidate()
        completed = self.run_cli("activate", "--profile", str(bootstrap))
        self.assertEqual(completed.returncode, 1)
        self.assertIn("--candidate is required", completed.stderr)


class PreflightCommandTests(CliTestCase):
    def test_requires_the_lab_confirmation(self) -> None:
        profile = self.lab.write_profile()
        completed = self.run_cli(
            "preflight", "--profile", str(profile), "--runs-dir", str(self.runs)
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("CEPH_INCIDENT_LAB_CONFIRM=1", completed.stderr)
        self.assertFalse(self.runs.exists())

    def test_writes_a_report_and_exits_zero_on_a_matching_lab(self) -> None:
        profile = self.lab.write_profile()
        completed = self.run_cli(
            "preflight",
            "--profile",
            str(profile),
            "--runs-dir",
            str(self.runs),
            CEPH_INCIDENT_LAB_CONFIRM="1",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ssh-fingerprints: ok", completed.stdout)
        latest = (self.runs / "LATEST").read_text(encoding="utf-8").strip()
        document = json.loads((self.runs / latest / "report.json").read_text("utf-8"))
        self.assertEqual(document["status"], "preflight-pass")
        self.assertIn("make validate-lab", self.next_action(completed))

    def test_an_unusable_profile_still_leaves_a_report(self) -> None:
        broken = self.lab.profiles / "broken.toml"
        broken.write_text("this is not a lab profile\n", encoding="utf-8")
        completed = self.run_cli(
            "preflight",
            "--profile",
            str(broken),
            "--runs-dir",
            str(self.runs),
            CEPH_INCIDENT_LAB_CONFIRM="1",
        )
        self.assertEqual(completed.returncode, 2)
        latest = (self.runs / "LATEST").read_text(encoding="utf-8").strip()
        document = json.loads((self.runs / latest / "report.json").read_text("utf-8"))
        self.assertEqual(document["status"], "profile-invalid")
        self.assertIsNone(document["profile"]["hash"])
        self.assertEqual(document["preflight"][0]["name"], "profile-load")
        self.assertIn("lab-preflight", self.next_action(completed))

    def test_a_mismatch_exits_two_and_still_writes_a_report(self) -> None:
        profile = self.lab.write_profile()
        completed = self.run_cli(
            "preflight",
            "--profile",
            str(profile),
            "--runs-dir",
            str(self.runs),
            CEPH_INCIDENT_LAB_CONFIRM="1",
            FAKE_LAB_CEPH_FSID=OTHER_FSID,
        )
        self.assertEqual(completed.returncode, 2)
        latest = (self.runs / "LATEST").read_text(encoding="utf-8").strip()
        document = json.loads((self.runs / latest / "report.json").read_text("utf-8"))
        self.assertEqual(document["status"], "ceph-identity-mismatch")
        self.assertIn("different Ceph cluster", self.next_action(completed))


class QualifyCommandTests(CliTestCase):
    """The `validate-lab` entry point.

    The gate's own orchestration is covered against the fake lab in
    `test_python_lab_qualify`; what the CLI owns is the confirmation, the run
    directory it reserves up front, and leaving a report either way.
    """

    def test_requires_the_lab_confirmation(self) -> None:
        profile = self.lab.write_profile()
        completed = self.run_cli(
            "qualify", "--profile", str(profile), "--runs-dir", str(self.runs)
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("CEPH_INCIDENT_LAB_CONFIRM=1", completed.stderr)
        self.assertFalse(self.runs.exists())

    def test_an_unusable_profile_still_leaves_a_report_in_the_run_directory(self) -> None:
        broken = self.lab.profiles / "broken.toml"
        broken.write_text("this is not a lab profile\n", encoding="utf-8")
        completed = self.run_cli(
            "qualify",
            "--profile",
            str(broken),
            "--runs-dir",
            str(self.runs),
            CEPH_INCIDENT_LAB_CONFIRM="1",
        )
        self.assertEqual(completed.returncode, 2)
        latest = (self.runs / "LATEST").read_text(encoding="utf-8").strip()
        document = json.loads((self.runs / latest / "report.json").read_text("utf-8"))
        self.assertEqual(document["status"], "profile-invalid")
        # The report lands in the directory reserved before the attempt started,
        # not in a second one created at write time.
        self.assertEqual(
            sorted(item.name for item in self.runs.iterdir()), sorted(["LATEST", latest])
        )

    def test_rejects_an_unusable_collect_timeout(self) -> None:
        for value in ("0", "-5", "soon"):
            with self.subTest(value=value):
                completed = self.run_cli(
                    "qualify",
                    "--profile",
                    "/labs/lab.toml",
                    "--collect-timeout",
                    value,
                    CEPH_INCIDENT_LAB_CONFIRM="1",
                )
                self.assertEqual(completed.returncode, 1)
                self.assertIn("positive number of seconds", completed.stderr)

    def test_the_collect_timeout_belongs_to_this_command_only(self) -> None:
        completed = self.run_cli(
            "preflight", "--profile", "/labs/lab.toml", "--collect-timeout", "60"
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("not valid for preflight", completed.stderr)


class CleanCommandTests(CliTestCase):
    """The `lab-clean` entry point: the only command that deletes anything.

    The purge's own rules are covered against a synthetic artifact root in
    `test_python_lab_artifacts`; what the CLI owns is the confirmation, refusing
    a profile, and naming the root outright.
    """

    def leave_run_artifacts(self, run_id: str) -> Path:
        run = self.runs / run_id
        (run / "shell").mkdir(parents=True)
        (run / "report.json").write_text("{}\n", encoding="utf-8")
        (run / "shell" / "collect.log").write_text("# exit: 2\n", encoding="utf-8")
        (run / "shell" / f"ceph-incident-{run_id}.tar.gz").write_bytes(b"\0" * 4096)
        return run

    def test_without_the_confirmation_it_previews_and_removes_nothing(self) -> None:
        run = self.leave_run_artifacts("20260801T230957Z")
        completed = self.run_cli("clean", "--runs-dir", str(self.runs), "--keep", "0")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("purge-not-confirmed", completed.stdout)
        self.assertIn("CEPH_INCIDENT_LAB_CLEAN=1", self.next_action(completed))
        self.assertTrue(
            (run / "shell" / "ceph-incident-20260801T230957Z.tar.gz").is_file()
        )

    def test_with_the_confirmation_it_reclaims_and_keeps_the_report(self) -> None:
        run = self.leave_run_artifacts("20260801T230957Z")
        completed = self.run_cli(
            "clean",
            "--runs-dir",
            str(self.runs),
            "--keep",
            "0",
            CEPH_INCIDENT_LAB_CLEAN="1",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("artifacts-purged", completed.stdout)
        self.assertFalse(
            (run / "shell" / "ceph-incident-20260801T230957Z.tar.gz").exists()
        )
        self.assertTrue((run / "report.json").is_file())
        self.assertTrue((run / "shell" / "collect.log").is_file())

    def test_the_default_keeps_the_most_recent_run(self) -> None:
        older = self.leave_run_artifacts("20260801T230957Z")
        newer = self.leave_run_artifacts("20260802T070709Z")
        completed = self.run_cli(
            "clean", "--runs-dir", str(self.runs), CEPH_INCIDENT_LAB_CLEAN="1"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((older / "shell" / "ceph-incident-20260801T230957Z.tar.gz").exists())
        self.assertTrue((newer / "shell" / "ceph-incident-20260802T070709Z.tar.gz").is_file())

    def test_a_lab_profile_cannot_take_part_in_deciding_what_is_deleted(self) -> None:
        profile = self.lab.write_profile()
        completed = self.run_cli(
            "clean", "--profile", str(profile), "--runs-dir", str(self.runs)
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("--profile is not valid for clean", completed.stderr)

    def test_the_artifact_root_must_be_named_absolutely(self) -> None:
        completed = self.run_cli("clean", "--runs-dir", "results/lab-validation")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("must be an absolute path", completed.stderr)

    def test_rejects_an_unusable_keep_count(self) -> None:
        for value in ("-1", "all", "1.5"):
            with self.subTest(value=value):
                completed = self.run_cli(
                    "clean", "--runs-dir", str(self.runs), "--keep", value
                )
                self.assertEqual(completed.returncode, 1)
                self.assertIn("count of runs to keep", completed.stderr)

    def test_the_keep_count_belongs_to_this_command_only(self) -> None:
        completed = self.run_cli(
            "status", "--profile", "/labs/lab.toml", "--keep", "1"
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("not valid for status", completed.stderr)

    def test_json_output_is_machine_readable(self) -> None:
        self.leave_run_artifacts("20260801T230957Z")
        completed = self.run_cli(
            "clean", "--runs-dir", str(self.runs), "--keep", "0", "--json"
        )
        document = json.loads(completed.stdout)
        self.assertEqual(document["status"], "purge-not-confirmed")
        self.assertEqual(
            [entry["run"] for entry in document["purged"]], ["20260801T230957Z"]
        )
        self.assertIn("next_action", document)


class WorkflowTests(CliTestCase):
    def test_a_fresh_agent_can_walk_the_whole_workflow_from_status(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        runs = ("--runs-dir", str(self.runs))
        first = self.run_cli("status", "--profile", str(bootstrap), *runs)
        self.assertIn("lab-profile-discover", first.stdout)
        self.assertEqual(
            self.run_cli("discover", "--profile", str(bootstrap)).returncode, 0
        )
        second = self.run_cli("status", "--profile", str(bootstrap), *runs)
        self.assertIn("candidate-pending-review", second.stdout)
        self.assertIn("lab-profile-activate", second.stdout)
        self.assertEqual(
            self.run_cli(
                "activate",
                "--profile",
                str(bootstrap),
                "--candidate",
                str(bootstrap.parent / "lab.candidate.toml"),
                CEPH_INCIDENT_LAB_ACTIVATE="1",
            ).returncode,
            0,
        )
        third = self.run_cli("status", "--profile", str(bootstrap), *runs)
        self.assertEqual(third.returncode, 0)
        self.assertIn("lab-preflight", third.stdout)
        self.assertEqual(
            self.run_cli(
                "preflight",
                "--profile",
                str(bootstrap),
                *runs,
                CEPH_INCIDENT_LAB_CONFIRM="1",
            ).returncode,
            0,
        )
        fourth = self.run_cli("status", "--profile", str(bootstrap), *runs)
        self.assertIn("preflight-passed", fourth.stdout)
        self.assertIn("make validate-lab", fourth.stdout)

    def test_a_rebuilt_lab_cannot_reach_the_preflight_without_review(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        self.run_cli("discover", "--profile", str(bootstrap))
        self.run_cli(
            "activate",
            "--profile",
            str(bootstrap),
            "--candidate",
            str(bootstrap.parent / "lab.candidate.toml"),
            CEPH_INCIDENT_LAB_ACTIVATE="1",
        )
        rebuilt = {
            "FAKE_LAB_CEPH_FSID": OTHER_FSID,
            "FAKE_LAB_HOST_KEYS": json.dumps(
                {"10.0.0.11": [["ssh-ed25519", "rebuilt-lab-key"]]}
            ),
        }
        blocked = self.run_cli(
            "preflight",
            "--profile",
            str(bootstrap),
            "--runs-dir",
            str(self.runs),
            CEPH_INCIDENT_LAB_CONFIRM="1",
            **rebuilt,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("ssh-fingerprint-mismatch", blocked.stdout)
        self.assertIn("never edit the recorded fingerprints", blocked.stdout)


if __name__ == "__main__":
    unittest.main()
