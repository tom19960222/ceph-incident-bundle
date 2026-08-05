"""Retained run artifacts: what is reported, what a purge takes, what survives.

The gate keeps a failed run's workdir on purpose, so the two things worth
pinning here are the opposite of each other: that the inventory sees everything
that piled up, and that the purge reaches nothing but the evidence it was aimed
at — not a report, not a ledger, not anything outside the artifact root it was
handed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from validation.lab_activation import ACTIVATION_LOG_NAME
from validation.lab_artifacts import (
    STATUS_INCOMPLETE,
    STATUS_NOT_CONFIRMED,
    STATUS_NOTHING_TO_PURGE,
    STATUS_PURGED,
    human_size,
    purge_artifacts,
    scan_artifacts,
)


class ArtifactsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "lab-validation"
        self.root.mkdir()

    def write_run(
        self,
        run_id: str,
        *,
        report: bool = True,
        implementations: tuple[str, ...] = ("shell", "python"),
        bundle_bytes: int = 1024,
        evidence: bool = True,
    ) -> Path:
        """Build one run directory shaped like a real `validate-lab` run."""

        run = self.root / run_id
        run.mkdir(mode=0o700, exist_ok=True)
        if report:
            (run / "report.json").write_text('{"status": "collect-failed"}\n', "utf-8")
            (run / "report.md").write_text("# Lab Validation Report\n", "utf-8")
        for implementation in implementations:
            output = run / implementation
            output.mkdir(exist_ok=True)
            (output / "collect.log").write_text("# exit: 2\n", encoding="utf-8")
            (output / "verify.log").write_text("VERIFY PASS\n", encoding="utf-8")
            if bundle_bytes:
                (output / f"ceph-incident-{run_id}.tar.gz").write_bytes(
                    b"\0" * bundle_bytes
                )
            if evidence:
                workdir = output / f"tmp.{run_id}.4242"
                (workdir / "nodes" / "osd01").mkdir(parents=True)
                (workdir / "nodes" / "osd01" / "var-log.tar.gz").write_bytes(b"\0" * 512)
        self.point_latest_at(run_id)
        return run

    def point_latest_at(self, run_id: str) -> None:
        (self.root / "LATEST").write_text(run_id + "\n", encoding="utf-8")

    def names(self, directory: Path) -> list[str]:
        return sorted(item.name for item in directory.iterdir())


class InventoryTests(ArtifactsTestCase):
    def test_an_absent_or_empty_root_reports_nothing(self) -> None:
        for root in (self.root, self.root / "never-created"):
            with self.subTest(root=root.name):
                inventory = scan_artifacts(root)
                self.assertEqual(inventory.count, 0)
                self.assertEqual(inventory.size, 0)
                self.assertIsNone(inventory.oldest)
                self.assertEqual(inventory.line(), "none retained")

    def test_counts_size_and_oldest_run_across_every_retained_run(self) -> None:
        self.write_run("20260801T230957Z", bundle_bytes=2048)
        self.write_run("20260802T070709Z", bundle_bytes=4096)
        inventory = scan_artifacts(self.root)
        self.assertEqual(inventory.count, 2)
        self.assertEqual([run.run_id for run in inventory.runs],
                         ["20260801T230957Z", "20260802T070709Z"])
        # Two implementations per run, each with one bundle and one 512-byte
        # evidence file; the reports and ledgers are not reclaimable.
        self.assertEqual(inventory.size, 2 * (2048 + 512) + 2 * (4096 + 512))
        self.assertEqual(inventory.oldest.run_id, "20260801T230957Z")
        self.assertEqual(inventory.oldest.timestamp, "2026-08-01T23:09:57Z")

    def test_a_report_only_run_is_not_a_retained_artifact(self) -> None:
        self.write_run("20260731T102621Z", implementations=())
        inventory = scan_artifacts(self.root)
        self.assertEqual(inventory.count, 0)

    def test_a_run_whose_directory_holds_only_ledgers_is_not_retained(self) -> None:
        self.write_run("20260731T102621Z", bundle_bytes=0, evidence=False)
        self.assertEqual(scan_artifacts(self.root).count, 0)

    def test_the_status_line_names_the_count_size_oldest_run_and_the_command(self) -> None:
        self.write_run("20260801T230957Z", bundle_bytes=1024 * 1024)
        line = scan_artifacts(self.root).line()
        self.assertIn("1 run(s)", line)
        self.assertIn("MiB", line)
        self.assertIn("20260801T230957Z", line)
        self.assertIn("make lab-clean", line)

    def test_a_run_id_with_a_collision_suffix_is_still_a_run(self) -> None:
        self.write_run("20260801T230957Z-2")
        inventory = scan_artifacts(self.root)
        self.assertEqual(inventory.count, 1)
        self.assertEqual(inventory.oldest.timestamp, "2026-08-01T23:09:57Z")

    def test_nothing_but_a_run_directory_is_ever_inventoried(self) -> None:
        self.write_run("20260801T230957Z")
        (self.root / "notes.md").write_text("scratch\n", encoding="utf-8")
        (self.root / "keep-me").mkdir()
        (self.root / "keep-me" / "big").write_bytes(b"\0" * 4096)
        inventory = scan_artifacts(self.root)
        self.assertEqual([run.run_id for run in inventory.runs], ["20260801T230957Z"])


class ConfirmationTests(ArtifactsTestCase):
    def test_an_unconfirmed_purge_removes_nothing_and_names_the_opt_in(self) -> None:
        self.write_run("20260801T230957Z")
        self.write_run("20260802T070709Z")
        before = scan_artifacts(self.root).size
        result = purge_artifacts(self.root, confirmed=False)
        self.assertEqual(result.status, STATUS_NOT_CONFIRMED)
        self.assertFalse(result.ok)
        self.assertEqual(result.reclaimed, 0)
        self.assertEqual(scan_artifacts(self.root).size, before)
        self.assertIn("CEPH_INCIDENT_LAB_CLEAN=1", result.next_action)
        self.assertIn("make lab-clean", result.next_action)
        self.assertNotIn("\n", result.next_action)

    def test_the_preview_lists_exactly_what_a_confirmed_run_would_take(self) -> None:
        self.write_run("20260801T230957Z")
        self.write_run("20260802T070709Z")
        preview = purge_artifacts(self.root, confirmed=False)
        self.assertEqual([run.run_id for run in preview.purged], ["20260801T230957Z"])
        self.assertEqual([run.run_id for run in preview.kept], ["20260802T070709Z"])
        done = purge_artifacts(self.root, confirmed=True)
        self.assertEqual(
            [run.run_id for run in done.purged], [run.run_id for run in preview.purged]
        )
        self.assertEqual(done.reclaimed, preview.reclaimable)

    def test_an_empty_root_needs_no_confirmation_to_report_nothing_to_do(self) -> None:
        result = purge_artifacts(self.root, confirmed=False)
        self.assertEqual(result.status, STATUS_NOTHING_TO_PURGE)
        self.assertTrue(result.ok)

    def test_a_negative_keep_is_refused_rather_than_treated_as_zero(self) -> None:
        with self.assertRaises(ValueError):
            purge_artifacts(self.root, confirmed=True, keep=-1)


class RetentionTests(ArtifactsTestCase):
    def test_the_most_recent_run_is_kept_by_default(self) -> None:
        self.write_run("20260801T230957Z")
        self.write_run("20260802T070709Z")
        newest = self.write_run("20260802T170828Z")
        result = purge_artifacts(self.root, confirmed=True)
        self.assertEqual(result.status, STATUS_PURGED)
        self.assertEqual([run.run_id for run in result.kept], ["20260802T170828Z"])
        self.assertEqual(scan_artifacts(self.root).count, 1)
        self.assertTrue((newest / "shell" / "ceph-incident-20260802T170828Z.tar.gz").is_file())

    def test_a_kept_run_is_the_most_recent_one_that_still_has_artifacts(self) -> None:
        # A later preflight leaves a report and nothing else.  Shielding that
        # would purge the failed run the operator is most likely still reading.
        failed = self.write_run("20260802T170828Z")
        self.write_run("20260803T080000Z", implementations=())
        purge_artifacts(self.root, confirmed=True)
        self.assertTrue((failed / "shell" / "ceph-incident-20260802T170828Z.tar.gz").is_file())

    def test_keeping_more_runs_than_exist_purges_nothing(self) -> None:
        self.write_run("20260801T230957Z")
        result = purge_artifacts(self.root, confirmed=True, keep=5)
        self.assertEqual(result.status, STATUS_NOTHING_TO_PURGE)
        self.assertEqual(scan_artifacts(self.root).count, 1)

    def test_keeping_none_purges_every_run_including_the_newest(self) -> None:
        self.write_run("20260801T230957Z")
        self.write_run("20260802T070709Z")
        result = purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertEqual(result.status, STATUS_PURGED)
        self.assertEqual(result.kept, ())
        self.assertEqual(scan_artifacts(self.root).count, 0)


class SurvivorTests(ArtifactsTestCase):
    def test_reports_and_command_ledgers_always_survive_a_purge(self) -> None:
        run = self.write_run("20260801T230957Z")
        purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertEqual(
            self.names(run), ["python", "report.json", "report.md", "shell"]
        )
        self.assertEqual(
            (run / "report.json").read_text("utf-8"), '{"status": "collect-failed"}\n'
        )
        for implementation in ("shell", "python"):
            self.assertEqual(
                self.names(run / implementation), ["collect.log", "verify.log"]
            )

    def test_the_run_directory_and_the_latest_pointer_survive(self) -> None:
        run = self.write_run("20260801T230957Z")
        purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertTrue(run.is_dir())
        self.assertEqual(
            (self.root / "LATEST").read_text("utf-8").strip(), "20260801T230957Z"
        )

    def test_an_implementation_directory_left_empty_is_pruned(self) -> None:
        run = self.write_run("20260801T230957Z", implementations=("shell",))
        (run / "shell" / "collect.log").unlink()
        (run / "shell" / "verify.log").unlink()
        purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertEqual(self.names(run), ["report.json", "report.md"])

    def test_a_run_the_gate_never_reported_is_still_purgeable(self) -> None:
        # A run killed before its report was written is the worst offender:
        # gigabytes with no verdict beside them.
        run = self.write_run("20260801T230957Z", report=False)
        result = purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertEqual(result.status, STATUS_PURGED)
        self.assertGreater(result.reclaimed, 0)
        self.assertTrue(run.is_dir())


class WriteBoundaryTests(ArtifactsTestCase):
    def test_nothing_outside_the_artifact_root_is_touched(self) -> None:
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (outside / "evidence.tar.gz").write_bytes(b"\0" * 4096)
        self.write_run("20260801T230957Z")
        purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertTrue((outside / "evidence.tar.gz").is_file())

    def test_the_activation_ledger_survives_even_inside_the_artifact_root(self) -> None:
        # It normally lives beside the Lab Profile, well outside any artifact
        # root, but it is qualification's audit trail either way: nothing that
        # is not a run directory is ever a removal target.
        ledger = self.root / ACTIVATION_LOG_NAME
        ledger.write_text('{"activated_at": "2026-07-31T10:26:03Z"}\n', encoding="utf-8")
        self.write_run("20260801T230957Z")
        purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertTrue(ledger.is_file())

    def test_a_symlinked_run_directory_is_neither_followed_nor_removed(self) -> None:
        outside = Path(self.directory.name) / "outside"
        (outside / "shell").mkdir(parents=True)
        (outside / "shell" / "evidence.tar.gz").write_bytes(b"\0" * 4096)
        link = self.root / "20260801T230957Z"
        link.symlink_to(outside, target_is_directory=True)
        self.assertEqual(scan_artifacts(self.root).count, 0)
        purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertTrue(link.is_symlink())
        self.assertTrue((outside / "shell" / "evidence.tar.gz").is_file())

    def test_a_symlink_inside_a_run_is_unlinked_and_its_target_survives(self) -> None:
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (outside / "evidence.tar.gz").write_bytes(b"\0" * 4096)
        run = self.write_run("20260801T230957Z", implementations=("shell",))
        link = run / "shell" / "bundle.tar.gz"
        link.symlink_to(outside / "evidence.tar.gz")
        purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertFalse(link.is_symlink())
        self.assertTrue((outside / "evidence.tar.gz").is_file())

    def test_a_symlink_wearing_a_reports_name_is_not_mistaken_for_one(self) -> None:
        outside = Path(self.directory.name) / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        run = self.write_run("20260801T230957Z", report=False, implementations=())
        (run / "report.json").symlink_to(outside)
        (run / "payload.tar.gz").write_bytes(b"\0" * 1024)
        purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertFalse((run / "report.json").is_symlink())
        self.assertTrue(outside.is_file())

    def test_an_unremovable_artifact_is_reported_rather_than_claimed_as_freed(self) -> None:
        run = self.write_run("20260801T230957Z", implementations=("shell",))
        (run / "shell").chmod(0o500)
        self.addCleanup(lambda: (run / "shell").chmod(0o700))
        result = purge_artifacts(self.root, confirmed=True, keep=0)
        self.assertEqual(result.status, STATUS_INCOMPLETE)
        self.assertFalse(result.ok)
        self.assertTrue(result.failures)
        self.assertIn("by hand", result.next_action)
        self.assertNotIn("\n", result.next_action)


class OutputTests(ArtifactsTestCase):
    def test_every_outcome_prints_exactly_one_next_action(self) -> None:
        self.write_run("20260801T230957Z")
        self.write_run("20260802T070709Z")
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                text = purge_artifacts(self.root, confirmed=confirmed).text()
                self.assertEqual(
                    len([line for line in text.splitlines()
                         if line.startswith("next action:")]),
                    1,
                )

    def test_the_summary_names_what_was_kept_purged_and_reclaimed(self) -> None:
        self.write_run("20260801T230957Z")
        self.write_run("20260802T070709Z")
        summary = purge_artifacts(self.root, confirmed=True).summary()
        self.assertEqual([entry["run"] for entry in summary["purged"]],
                         ["20260801T230957Z"])
        self.assertEqual([entry["run"] for entry in summary["kept"]],
                         ["20260802T070709Z"])
        self.assertGreater(summary["reclaimed_bytes"], 0)
        self.assertEqual(summary["status"], STATUS_PURGED)
        self.assertTrue(summary["confirmed"])

    def test_sizes_are_rendered_in_the_binary_units_du_reports(self) -> None:
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(1024), "1.0 KiB")
        self.assertEqual(human_size(3 * 1024 ** 3), "3.0 GiB")
        self.assertEqual(human_size(2048 * 1024 ** 4), "2048.0 TiB")


if __name__ == "__main__":
    unittest.main()
