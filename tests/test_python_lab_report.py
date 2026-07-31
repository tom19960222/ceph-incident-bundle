from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import (
    CEPH_FSID,
    ROOK_FSID,
    FakeLab,
    fake_entrypoints,
    host_fingerprint,
)
from validation.lab_preflight import preflight
from validation.lab_qualify import qualify
from validation.lab_report import (
    COLLECTOR_PATHS,
    LATEST_NAME,
    REPORT_SCHEMA_VERSION,
    CodeIdentity,
    CollectorCoverage,
    ComparisonRecord,
    LabValidationReport,
    ReportRejected,
    ResidueRecord,
    RunRecord,
    StableStateRecord,
    latest_report,
    report_from_preflight,
    report_from_qualification,
    report_from_unusable_profile,
    write_report,
)


CODE = CodeIdentity(commit="0123456789abcdef0123456789abcdef01234567", dirty=False)


def minimal_report(**overrides: object) -> LabValidationReport:
    fields: dict[str, object] = {
        "timestamp": "2026-07-30T00:00:00Z",
        "code": CODE,
        "profile_display": "~/labs/lab.toml",
        "profile_hash": "sha256:" + "a" * 64,
        "profile_state": "active",
        "profile_name": "lab",
        "status": "preflight-pass",
        "next_action": "Hand off this report",
    }
    fields.update(overrides)
    return LabValidationReport(**fields)  # type: ignore[arg-type]


class SchemaTests(unittest.TestCase):
    def test_the_document_declares_every_required_section(self) -> None:
        document = minimal_report().document()
        self.assertEqual(document["schema_version"], REPORT_SCHEMA_VERSION)
        for key in (
            "timestamp",
            "code",
            "profile",
            "lab_identity",
            "preflight",
            "runs",
            "comparison",
            "stable_state",
            "residue",
            "status",
            "next_action",
        ):
            self.assertIn(key, document)

    def test_unfilled_sections_say_not_run_rather_than_pass(self) -> None:
        document = minimal_report(
            runs=(RunRecord("shell"), RunRecord("python")),
            residue=(ResidueRecord("monitor01"),),
        ).document()
        self.assertEqual(document["comparison"]["result"], "not-run")
        self.assertEqual(document["stable_state"]["result"], "not-run")
        self.assertEqual(document["residue"][0]["result"], "not-run")
        for run in document["runs"]:
            self.assertEqual(run["verify_result"], "not-run")
            self.assertEqual(
                sorted(run["coverage"]), sorted(COLLECTOR_PATHS)
            )
            self.assertEqual(set(run["coverage"].values()), {"not-run"})

    def test_coverage_is_only_complete_when_every_path_was_collected(self) -> None:
        partial = CollectorCoverage(
            ceph="collected",
            rook="collected",
            prometheus="collected",
            nodes="collected",
        )
        self.assertFalse(partial.complete)
        self.assertTrue(
            CollectorCoverage(
                *(["collected"] * len(COLLECTOR_PATHS))
            ).complete
        )

    def test_a_filled_report_round_trips_through_json(self) -> None:
        report = minimal_report(
            status="pass",
            next_action="Proceed to the cutover ticket",
            runs=(
                RunRecord(
                    "shell",
                    exit_code=0,
                    invocation_id="run-a",
                    bundle_path="/tmp/a.tar.gz",
                    bundle_hash="sha256:aa",
                    verify_result="pass",
                    coverage=CollectorCoverage(*(["collected"] * len(COLLECTOR_PATHS))),
                ),
            ),
            comparison=ComparisonRecord(result="match"),
            stable_state=StableStateRecord(schema_version=1, result="unchanged"),
            residue=(ResidueRecord("monitor01", result="clean", detail="no workspace"),),
        )
        document = json.loads(json.dumps(report.document()))
        self.assertTrue(report.passed)
        self.assertEqual(document["runs"][0]["coverage"]["var_log"], "collected")
        self.assertEqual(document["residue"][0]["result"], "clean")


class MarkdownTests(unittest.TestCase):
    def test_markdown_and_json_state_the_same_result(self) -> None:
        report = minimal_report(status="stable-state-diff", next_action="Review the diff")
        markdown = report.markdown()
        self.assertIn("- status: stable-state-diff", markdown)
        self.assertIn("- next action: Review the diff", markdown)
        self.assertIn(report.profile_hash, markdown)
        self.assertIn(CODE.commit, markdown)

    def test_markdown_carries_every_section_heading(self) -> None:
        markdown = minimal_report().markdown()
        for heading in (
            "## Status",
            "## Lab identity",
            "## Preflight",
            "## Full collect runs",
            "## Bundle comparison",
            "## Stable state",
            "## Remote residue",
        ):
            self.assertIn(heading, markdown)

    def test_a_detail_containing_a_pipe_cannot_break_the_table(self) -> None:
        report = minimal_report(
            preflight=({"name": "ceph-identity", "ok": False, "detail": "a | b"},)
        )
        row = [line for line in report.markdown().splitlines() if "ceph-identity" in line][0]
        self.assertIn("a \\| b", row)
        self.assertEqual(row.count(" | "), 2)


class WriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runs = Path(self.directory.name) / "lab-validation"

    def test_writes_both_formats_and_a_latest_pointer(self) -> None:
        location = write_report(self.runs, minimal_report())
        self.assertTrue(location.json_path.is_file())
        self.assertTrue(location.markdown_path.is_file())
        self.assertEqual(
            location.latest_path.read_text(encoding="utf-8").strip(),
            location.directory.name,
        )

    def test_report_files_are_owner_only(self) -> None:
        location = write_report(self.runs, minimal_report())
        self.assertEqual(location.directory.stat().st_mode & 0o077, 0)
        self.assertEqual(location.json_path.stat().st_mode & 0o077, 0)
        self.assertEqual(location.markdown_path.stat().st_mode & 0o077, 0)

    def test_a_second_run_in_the_same_second_gets_its_own_directory(self) -> None:
        first = write_report(self.runs, minimal_report())
        second = write_report(self.runs, minimal_report())
        self.assertNotEqual(first.directory, second.directory)
        self.assertEqual(
            second.latest_path.read_text(encoding="utf-8").strip(), second.directory.name
        )
        self.assertTrue(first.json_path.is_file())

    def test_refuses_a_report_without_exactly_one_next_action(self) -> None:
        for action in ("", "   ", "first\nsecond"):
            with self.subTest(action=action):
                with self.assertRaises(ReportRejected):
                    write_report(self.runs, minimal_report(next_action=action))

    def test_refuses_a_report_without_a_status(self) -> None:
        with self.assertRaises(ReportRejected):
            write_report(self.runs, minimal_report(status=" "))

    def test_refuses_to_write_a_report_that_carries_credential_material(self) -> None:
        leaky = minimal_report(
            preflight=(
                {
                    "name": "ceph-identity",
                    "ok": False,
                    "detail": "-----BEGIN OPENSSH PRIVATE KEY-----",
                },
            )
        )
        with self.assertRaises(ReportRejected) as raised:
            write_report(self.runs, leaky)
        self.assertIn("credential material", str(raised.exception))
        self.assertFalse((self.runs / LATEST_NAME).exists())

    def test_refuses_a_report_that_carries_an_authorization_header(self) -> None:
        with self.assertRaises(ReportRejected):
            write_report(
                self.runs,
                minimal_report(next_action="Retry with Authorization: Bearer abc"),
            )


class LatestPointerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runs = Path(self.directory.name) / "lab-validation"

    def test_reads_back_the_most_recent_report(self) -> None:
        write_report(self.runs, minimal_report(status="ceph-identity-mismatch"))
        location = write_report(self.runs, minimal_report(status="preflight-pass"))
        document = latest_report(self.runs)
        self.assertIsNotNone(document)
        self.assertEqual(document["status"], "preflight-pass")
        self.assertEqual(document["directory"], str(location.directory))

    def test_reports_nothing_when_there_is_no_pointer(self) -> None:
        self.assertIsNone(latest_report(self.runs))

    def test_ignores_a_pointer_that_escapes_the_runs_directory(self) -> None:
        self.runs.mkdir(parents=True)
        (self.runs / LATEST_NAME).write_text("../elsewhere\n", encoding="utf-8")
        self.assertIsNone(latest_report(self.runs))

    def test_ignores_a_pointer_to_a_missing_report(self) -> None:
        self.runs.mkdir(parents=True)
        (self.runs / LATEST_NAME).write_text("20260730T000000Z\n", encoding="utf-8")
        self.assertIsNone(latest_report(self.runs))


class PreflightReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lab = FakeLab(Path(self.directory.name))
        self.runs = Path(self.directory.name) / "lab-validation"

    def test_builds_an_honest_report_from_a_passing_preflight(self) -> None:
        with mock.patch.dict(os.environ, self.lab.environment()):
            result = preflight(self.lab.write_profile())
        report = report_from_preflight(
            result, code=CODE, hosts=("monitor01", "mon02", "osd01")
        )
        document = write_report(self.runs, report).json_path.read_text(encoding="utf-8")
        parsed = json.loads(document)
        self.assertEqual(parsed["status"], "preflight-pass")
        self.assertEqual(parsed["lab_identity"]["ceph_fsid"], CEPH_FSID)
        self.assertEqual(parsed["lab_identity"]["rook_fsid"], ROOK_FSID)
        self.assertEqual(
            parsed["lab_identity"]["hosts"][0]["ssh_fingerprints_verified"],
            [host_fingerprint("10.0.0.11")],
        )
        self.assertEqual([run["implementation"] for run in parsed["runs"]], ["shell", "python"])
        self.assertEqual(parsed["comparison"]["result"], "not-run")
        self.assertEqual(len(parsed["residue"]), 3)
        self.assertIn("make validate-lab", parsed["next_action"])

    def test_a_failing_preflight_reports_its_failure_class(self) -> None:
        with mock.patch.dict(
            os.environ, self.lab.environment(FAKE_LAB_PROM_MODE="down")
        ):
            result = preflight(self.lab.write_profile())
        report = report_from_preflight(result, code=CODE)
        self.assertEqual(report.status, "prometheus-not-ready")
        self.assertFalse(report.passed)
        markdown = write_report(self.runs, report).markdown_path.read_text("utf-8")
        self.assertIn("prometheus-not-ready", markdown)
        self.assertIn("FAILED", markdown)

    def test_an_attempt_that_could_not_load_its_profile_still_reports(self) -> None:
        report = report_from_unusable_profile(
            Path("/labs/lab.toml"),
            "missing lab profile: /labs/lab.toml",
            code=CODE,
            status="profile-invalid",
            next_action="Create the profile and re-run the preflight",
        )
        location = write_report(self.runs, report)
        document = json.loads(location.json_path.read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "profile-invalid")
        self.assertIsNone(document["profile"]["hash"])
        self.assertIsNone(document["profile"]["state"])
        self.assertEqual(document["preflight"][0]["name"], "profile-load")
        self.assertFalse(document["preflight"][0]["ok"])
        self.assertFalse(report.passed)
        markdown = location.markdown_path.read_text(encoding="utf-8")
        self.assertIn("profile-load", markdown)
        self.assertIn("no lab identity was verified", markdown)
        self.assertIn("- profile hash: unknown", markdown)
        self.assertNotIn("None", markdown)

    def test_the_written_report_carries_no_credential_content(self) -> None:
        with mock.patch.dict(os.environ, self.lab.environment()):
            result = preflight(self.lab.write_profile())
        location = write_report(self.runs, report_from_preflight(result, code=CODE))
        for path in (location.json_path, location.markdown_path):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("-----BEGIN", text)
            self.assertNotIn("placeholder", text)


class QualificationReportTests(unittest.TestCase):
    """One dual-run attempt's report, written from the harness's own result."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lab = FakeLab(self.root)
        self.runs = self.root / "lab-validation"
        self.runs.mkdir()

    def run_gate(self, **knobs: str):
        run_directory = self.runs / "20260731T000000Z"
        run_directory.mkdir(mode=0o700)
        with mock.patch.dict(os.environ, self.lab.environment(**knobs)):
            return qualify(
                self.lab.write_profile(),
                run_directory=run_directory,
                entrypoints=fake_entrypoints(),
                collect_timeout=120,
            )

    def write(self, result):
        return write_report(
            self.runs,
            report_from_qualification(result, code=CODE),
            directory=result.run_directory,
        )

    def test_a_pass_fills_every_section_and_points_LATEST_at_the_run(self) -> None:
        result = self.run_gate()
        location = self.write(result)
        self.assertEqual(location.directory, result.run_directory)
        document = json.loads(location.json_path.read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "pass")
        self.assertEqual(document["schema_version"], REPORT_SCHEMA_VERSION)
        self.assertEqual(
            [run["implementation"] for run in document["runs"]], ["shell", "python"]
        )
        for run in document["runs"]:
            self.assertEqual(run["verify_result"], "pass")
            self.assertEqual(
                sorted(run["coverage"]), sorted(COLLECTOR_PATHS)
            )
            self.assertTrue(all(value == "collected" for value in run["coverage"].values()))
        self.assertEqual(document["comparison"]["result"], "equivalent")
        self.assertEqual(document["stable_state"]["result"], "unchanged")
        self.assertEqual(document["stable_state"]["snapshot_schema_version"], 1)
        self.assertEqual(len(document["residue"]), 3)
        self.assertEqual(
            (self.runs / LATEST_NAME).read_text(encoding="utf-8").strip(),
            result.run_directory.name,
        )

    def test_markdown_and_json_say_the_same_thing(self) -> None:
        location = self.write(self.run_gate())
        markdown = location.markdown_path.read_text(encoding="utf-8")
        document = json.loads(location.json_path.read_text(encoding="utf-8"))
        self.assertIn(f"- status: {document['status']}", markdown)
        self.assertIn(f"- next action: {document['next_action']}", markdown)
        self.assertIn("| shell |", markdown)
        self.assertIn("| python |", markdown)
        self.assertIn("- result: equivalent", markdown)
        self.assertIn("| monitor01 | clean |", markdown)

    def test_a_failure_records_where_the_gate_stopped(self) -> None:
        result = self.run_gate(FAKE_COLLECT_DROP_python="prometheus")
        document = json.loads(self.write(result).json_path.read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "coverage-incomplete")
        self.assertEqual(document["runs"][1]["coverage"]["prometheus"], "missing")
        # The gate stopped before comparing, and the report says so rather than
        # implying the later stages passed.
        self.assertEqual(document["comparison"]["result"], "not-run")
        self.assertEqual(document["stable_state"]["result"], "not-run")
        self.assertTrue(all(entry["result"] == "not-run" for entry in document["residue"]))

    def test_a_qualification_report_carries_no_credential_content(self) -> None:
        location = self.write(self.run_gate())
        for path in (location.json_path, location.markdown_path):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("-----BEGIN", text)
            self.assertNotIn("ssh-ed25519 AAAA", text)
            self.assertNotIn("placeholder", text)


if __name__ == "__main__":
    unittest.main()
